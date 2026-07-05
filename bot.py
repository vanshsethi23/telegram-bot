#!/usr/bin/env python3
"""Telegram media repost bot.

Forward a video/photo/document to the bot and it will:
  1. strip your configured keywords from the caption (case-insensitive),
  2. add your replacement text (append/prepend/replace),
  3. optionally attach a new thumbnail to videos (ffmpeg stream-copy, no transcode),
  4. post the result to your configured target channel.

Config is stored per-user in config.json next to this file.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

from telegram import Message, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("repost-bot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
THUMB_DIR = os.path.join(BASE_DIR, "thumbnails")

CAPTION_LIMIT = 1024  # Telegram caption limit (chars)
BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API can't download files > 20 MB
BOT_UPLOAD_LIMIT = 50 * 1024 * 1024  # Bot API can't upload files > 50 MB
THUMB_MAX_SIDE = 320  # Telegram thumbnail requirement

VALID_MODES = ("append", "prepend", "replace")

# ---------------------------------------------------------------- persistence


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


CONFIG = load_config()


def user_cfg(user_id: int) -> dict:
    return CONFIG.setdefault(
        str(user_id),
        {
            "keywords": [],
            "replacement": "",
            "mode": "append",
            "target_chat_id": None,
            "target_title": None,
            "thumb_path": None,
        },
    )


# ------------------------------------------------------------ caption editing


def clean_caption(caption: str, keywords: list[str]) -> str:
    text = caption
    for kw in keywords:
        if kw:
            text = re.sub(re.escape(kw), "", text, flags=re.IGNORECASE)
    # tidy up leftover whitespace
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_caption(original: str, cfg: dict) -> str:
    cleaned = clean_caption(original or "", cfg["keywords"])
    repl = cfg["replacement"]
    mode = cfg.get("mode", "append")
    if mode == "replace" and repl:
        final = repl
    elif mode == "prepend":
        final = "\n\n".join(p for p in (repl, cleaned) if p)
    else:  # append
        final = "\n\n".join(p for p in (cleaned, repl) if p)
    return final[:CAPTION_LIMIT]


# ------------------------------------------------------------------- ffmpeg


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def attach_thumbnail(video_path: str, thumb_path: str, out_path: str) -> bool:
    """Embed thumb as attached_pic via stream copy (no transcode). True on success."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video_path, "-i", thumb_path,
        "-map", "0", "-map", "1",
        "-c", "copy",
        "-disposition:v:1", "attached_pic",
        out_path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("ffmpeg failed: %s", e)
        return False
    if res.returncode != 0:
        log.warning("ffmpeg failed: %s", res.stderr.strip())
        return False
    return True


def make_telegram_thumb(src_path: str, out_path: str) -> bool:
    """Convert an image to a Telegram-compliant thumbnail (JPEG, <=320px)."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", src_path,
        "-vf", f"scale='min({THUMB_MAX_SIDE},iw)':'min({THUMB_MAX_SIDE},ih)'"
               ":force_original_aspect_ratio=decrease",
        "-frames:v", "1", "-q:v", "3",
        out_path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return res.returncode == 0 and os.path.exists(out_path)


# ----------------------------------------------------------------- commands

HELP_TEXT = (
    "Forward me a video/photo/document and I'll clean its caption and repost it "
    "to your target channel.\n\n"
    "Configuration commands:\n"
    "/setkeywords word1, word2, ... — keywords to strip from captions\n"
    "/clearkeywords — remove all keywords\n"
    "/setcaption <text> — replacement text to add\n"
    "/clearcaption — remove replacement text\n"
    "/setmode append|prepend|replace — how the replacement text is applied "
    "(default: append after the cleaned caption)\n"
    "/settarget @channel or -100... id — target channel (bot must be admin)\n"
    "/setthumb — then send a photo (or send a photo with caption /setthumb) "
    "to use as thumbnail for videos\n"
    "/clearthumb — stop replacing video thumbnails\n"
    "/status — show current configuration"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_setkeywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = user_cfg(update.effective_user.id)
    raw = update.message.text.partition(" ")[2]
    keywords = [k.strip() for k in raw.split(",") if k.strip()]
    if not keywords:
        await update.message.reply_text(
            "Usage: /setkeywords word1, word2, some phrase"
        )
        return
    cfg["keywords"] = keywords
    save_config(CONFIG)
    await update.message.reply_text(
        "Keywords to remove:\n" + "\n".join(f"• {k}" for k in keywords)
    )


async def cmd_clearkeywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = user_cfg(update.effective_user.id)
    cfg["keywords"] = []
    save_config(CONFIG)
    await update.message.reply_text("Keyword list cleared.")


async def cmd_setcaption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = user_cfg(update.effective_user.id)
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /setcaption Your replacement text")
        return
    cfg["replacement"] = text
    save_config(CONFIG)
    await update.message.reply_text(f"Replacement text set:\n{text}")


async def cmd_clearcaption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = user_cfg(update.effective_user.id)
    cfg["replacement"] = ""
    save_config(CONFIG)
    await update.message.reply_text("Replacement text cleared.")


async def cmd_setmode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = user_cfg(update.effective_user.id)
    mode = update.message.text.partition(" ")[2].strip().lower()
    if mode not in VALID_MODES:
        await update.message.reply_text(
            "Usage: /setmode append|prepend|replace\n"
            "append — cleaned caption, then your text\n"
            "prepend — your text, then cleaned caption\n"
            "replace — only your text"
        )
        return
    cfg["mode"] = mode
    save_config(CONFIG)
    await update.message.reply_text(f"Caption mode: {mode}")


async def _verify_admin(bot, chat_id) -> tuple[bool, str]:
    """Return (ok, error_message)."""
    try:
        chat = await bot.get_chat(chat_id)
        member = await bot.get_chat_member(chat.id, bot.id)
    except Forbidden:
        return False, (
            "I can't access that chat. Add me to the channel as an administrator "
            "with the 'Post messages' right, then try again."
        )
    except BadRequest as e:
        return False, (
            f"Couldn't find that chat ({e.message}). Check the @username / id, and make "
            "sure I've been added to the channel as an administrator."
        )
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return False, (
            f"I'm in “{chat.title}” but I'm not an administrator there. "
            "Promote me with the 'Post messages' right, then run /settarget again."
        )
    if member.status == ChatMemberStatus.ADMINISTRATOR and not member.can_post_messages:
        return False, (
            f"I'm an admin in “{chat.title}” but I don't have the "
            "'Post messages' right. Enable it, then run /settarget again."
        )
    return True, ""


async def cmd_settarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = user_cfg(update.effective_user.id)
    arg = update.message.text.partition(" ")[2].strip()
    if not arg:
        await update.message.reply_text(
            "Usage: /settarget @channelusername or /settarget -1001234567890"
        )
        return

    # normalize t.me links
    m = re.match(r"(?:https?://)?t\.me/(.+)", arg)
    if m:
        arg = m.group(1).strip("/")
        if arg.startswith("+") or arg.startswith("joinchat"):
            await update.message.reply_text(
                "That's a private invite link — bots can't join channels via invite "
                "links. Add me to the channel as an admin yourself, then send "
                "/settarget with the channel's @username, or (for private channels) "
                "its numeric id like -1001234567890.\n"
                "Tip: forward any post from the channel to @userinfobot to get the id."
            )
            return

    if re.fullmatch(r"-?\d+", arg):
        target = int(arg)
    else:
        target = arg if arg.startswith("@") else "@" + arg

    ok, err = await _verify_admin(context.bot, target)
    if not ok:
        await update.message.reply_text(f"❌ {err}")
        return

    chat = await context.bot.get_chat(target)
    cfg["target_chat_id"] = chat.id
    cfg["target_title"] = chat.title or str(target)
    save_config(CONFIG)
    await update.message.reply_text(
        f"✅ Target channel set: {cfg['target_title']} (id {chat.id}). "
        "I verified that I'm an admin and can post there."
    )


async def cmd_setthumb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /setthumb as a reply to a photo works immediately
    reply = update.message.reply_to_message
    if reply and reply.photo:
        await _store_thumbnail(update.message, reply, context)
        return
    context.user_data["awaiting_thumb"] = True
    await update.message.reply_text(
        "Send me the photo to use as the new video thumbnail."
    )


async def cmd_clearthumb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = user_cfg(update.effective_user.id)
    if cfg.get("thumb_path") and os.path.exists(cfg["thumb_path"]):
        os.remove(cfg["thumb_path"])
    cfg["thumb_path"] = None
    save_config(CONFIG)
    await update.message.reply_text("Thumbnail cleared — videos keep their own thumbnail.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = user_cfg(update.effective_user.id)
    kw = ", ".join(cfg["keywords"]) or "(none)"
    await update.message.reply_text(
        f"Keywords to remove: {kw}\n"
        f"Replacement text: {cfg['replacement'] or '(none)'}\n"
        f"Caption mode: {cfg.get('mode', 'append')}\n"
        f"Target channel: {cfg['target_title'] or '(not set)'}\n"
        f"Custom thumbnail: {'set' if cfg.get('thumb_path') else '(none)'}\n"
        f"ffmpeg: {'available' if ffmpeg_available() else 'NOT FOUND (thumbnail embedding disabled)'}"
    )


async def _store_thumbnail(
    status_msg: Message, photo_msg: Message, context: ContextTypes.DEFAULT_TYPE
) -> None:
    cfg = user_cfg(photo_msg.from_user.id if photo_msg.from_user else status_msg.chat_id)
    os.makedirs(THUMB_DIR, exist_ok=True)
    photo = photo_msg.photo[-1]
    tg_file = await photo.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw.jpg")
        await tg_file.download_to_drive(raw)
        dest = os.path.join(THUMB_DIR, f"{status_msg.chat_id}.jpg")
        if ffmpeg_available():
            if not make_telegram_thumb(raw, dest):
                await status_msg.reply_text("❌ Couldn't process that image, try another one.")
                return
        else:
            shutil.copy(raw, dest)  # Telegram photos are already JPEG
    cfg["thumb_path"] = dest
    save_config(CONFIG)
    await status_msg.reply_text(
        "✅ Thumbnail saved. It will be applied to videos you forward from now on."
    )


# ------------------------------------------------------------- media handler


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user_id = update.effective_user.id
    cfg = user_cfg(user_id)

    # photo sent right after /setthumb (or with /setthumb as caption) sets the thumbnail
    if msg.photo and (
        context.user_data.pop("awaiting_thumb", False)
        or (msg.caption or "").strip().lower().startswith("/setthumb")
    ):
        await _store_thumbnail(msg, msg, context)
        return

    if not cfg["target_chat_id"]:
        await msg.reply_text("No target channel configured. Use /settarget first.")
        return

    ok, err = await _verify_admin(context.bot, cfg["target_chat_id"])
    if not ok:
        await msg.reply_text(f"❌ Can't post to {cfg['target_title']}: {err}")
        return

    final_caption = build_caption(msg.caption or "", cfg)

    try:
        if msg.video and cfg.get("thumb_path"):
            await _repost_video_with_thumb(msg, cfg, final_caption, context)
        else:
            await context.bot.copy_message(
                chat_id=cfg["target_chat_id"],
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=final_caption,
            )
            await msg.reply_text(f"✅ Posted to {cfg['target_title']}.")
    except Forbidden:
        await msg.reply_text(
            f"❌ Posting failed: I've lost posting rights in {cfg['target_title']}. "
            "Make sure I'm still an admin with 'Post messages'."
        )
    except BadRequest as e:
        await msg.reply_text(f"❌ Telegram rejected the post: {e.message}")
    except TelegramError as e:
        await msg.reply_text(f"❌ Telegram error: {e}")


async def _repost_video_with_thumb(
    msg: Message, cfg: dict, caption: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download video, embed the configured thumbnail without transcoding, upload."""
    video = msg.video
    target = cfg["target_chat_id"]

    async def copy_without_thumb(reason: str) -> None:
        await context.bot.copy_message(
            chat_id=target, from_chat_id=msg.chat_id,
            message_id=msg.message_id, caption=caption,
        )
        await msg.reply_text(
            f"⚠️ Posted to {cfg['target_title']} with the original thumbnail: {reason}"
        )

    if video.file_size and video.file_size > BOT_DOWNLOAD_LIMIT:
        await copy_without_thumb(
            "the Bot API only lets bots download files up to 20 MB, so I can't "
            f"re-upload this {video.file_size / 1024 / 1024:.0f} MB video with a new thumbnail."
        )
        return
    if not ffmpeg_available():
        await copy_without_thumb("ffmpeg is not installed on the bot's machine.")
        return

    note = await msg.reply_text("⏳ Replacing thumbnail and uploading…")
    with tempfile.TemporaryDirectory() as tmp:
        ext = os.path.splitext(video.file_name or "")[1] or ".mp4"
        src = os.path.join(tmp, "in" + ext)
        out = os.path.join(tmp, "out.mp4")
        try:
            tg_file = await video.get_file()
            await tg_file.download_to_drive(src)
        except BadRequest as e:
            await note.delete()
            await copy_without_thumb(f"Telegram wouldn't let me download the file ({e.message}).")
            return

        upload_path = src
        loop = asyncio.get_running_loop()
        embedded = await loop.run_in_executor(
            None, attach_thumbnail, src, cfg["thumb_path"], out
        )
        if embedded:
            upload_path = out
        # embedding can fail on exotic containers; the send_video `thumbnail`
        # parameter below still sets the visible Telegram thumbnail either way

        if os.path.getsize(upload_path) > BOT_UPLOAD_LIMIT:
            await note.delete()
            await copy_without_thumb(
                "the result exceeds the Bot API's 50 MB upload limit."
            )
            return

        with open(upload_path, "rb") as vf, open(cfg["thumb_path"], "rb") as tf:
            await context.bot.send_video(
                chat_id=target,
                video=vf,
                caption=caption,
                thumbnail=tf,
                duration=video.duration,
                width=video.width,
                height=video.height,
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
            )
    await note.delete()
    extra = "" if embedded else " (thumbnail set via Telegram; embedding in the file itself failed)"
    await msg.reply_text(f"✅ Posted to {cfg['target_title']} with the new thumbnail.{extra}")


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Send me a video, photo or document to repost, or /help for commands."
        )


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable (get one from @BotFather).")
    if not ffmpeg_available():
        log.warning("ffmpeg not found — thumbnail embedding will be skipped.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("setkeywords", cmd_setkeywords))
    app.add_handler(CommandHandler("clearkeywords", cmd_clearkeywords))
    app.add_handler(CommandHandler("setcaption", cmd_setcaption))
    app.add_handler(CommandHandler("clearcaption", cmd_clearcaption))
    app.add_handler(CommandHandler("setmode", cmd_setmode))
    app.add_handler(CommandHandler("settarget", cmd_settarget))
    app.add_handler(CommandHandler("setthumb", cmd_setthumb))
    app.add_handler(CommandHandler("clearthumb", cmd_clearthumb))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(
        MessageHandler(
            (filters.VIDEO | filters.PHOTO | filters.Document.ALL | filters.ANIMATION)
            & filters.ChatType.PRIVATE,
            handle_media,
        )
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_other)
    )

    log.info("Bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
