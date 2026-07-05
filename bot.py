#!/usr/bin/env python3
"""Telegram media repost bot (Pyrogram / MTProto).

Forward a video/photo/document to the bot and it will:
  1. strip your configured keywords from the caption (case-insensitive),
  2. add your replacement text (append/prepend/replace),
  3. optionally attach a new thumbnail to videos (ffmpeg stream-copy, no transcode),
  4. post the result to your configured target channel.

Uses Pyrogram (MTProto) instead of the plain Bot API so file transfers are not
capped at the Bot API's 20 MB download / 50 MB upload limits - files up to
Telegram's own ~2 GB limit work directly.

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

from pyrogram import Client, enums, filters
from pyrogram.errors import RPCError
from pyrogram.types import Message

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("repost-bot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
THUMB_DIR = os.path.join(BASE_DIR, "thumbnails")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

CAPTION_LIMIT = 1024  # Telegram caption limit (chars)
TEXT_LIMIT = 4096  # Telegram text message limit (chars)
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # Telegram's own upload ceiling (2 GB, non-premium)
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
AWAITING_THUMB = set()  # user ids that just ran /setthumb and need to send a photo
REPOST_QUEUE = asyncio.Queue()  # FIFO of (client, message) awaiting repost
WORKER_TASK = None


def ensure_worker() -> None:
    """Start (or restart after a crash) the single FIFO repost worker."""
    global WORKER_TASK
    if WORKER_TASK is None or WORKER_TASK.done():
        WORKER_TASK = asyncio.get_running_loop().create_task(repost_worker())


async def repost_worker() -> None:
    """Process queued reposts one at a time, in arrival order."""
    while True:
        client, message = await REPOST_QUEUE.get()
        try:
            await process_repost(client, message)
        except Exception:
            log.exception("Repost failed for message %s", message.id)
            try:
                await message.reply_text("❌ Something went wrong reposting this file.")
            except Exception:
                pass
        finally:
            REPOST_QUEUE.task_done()


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


def clean_caption(caption: str, keywords: list) -> str:
    text = caption
    for kw in keywords:
        if kw:
            text = re.sub(re.escape(kw), "", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_caption(original: str, cfg: dict, limit: int = CAPTION_LIMIT) -> str:
    cleaned = clean_caption(original or "", cfg["keywords"])
    repl = cfg["replacement"]
    mode = cfg.get("mode", "append")
    if mode == "replace" and repl:
        final = repl
    elif mode == "prepend":
        final = "\n\n".join(p for p in (repl, cleaned) if p)
    else:  # append
        final = "\n\n".join(p for p in (cleaned, repl) if p)
    return final[:limit]


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
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
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


# ----------------------------------------------------------------- helpers

HELP_TEXT = (
    "Forward me a video/photo/document/text and I'll clean its caption (or text) "
    "and repost it to your target channel.\n\n"
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
    "/status — show current configuration\n\n"
    "Files up to Telegram's own ~2 GB limit are supported."
)


def command_arg(message: Message) -> str:
    parts = message.text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def verify_admin(app: Client, chat_id) -> tuple:
    """Return (ok, error_message)."""
    try:
        chat = await app.get_chat(chat_id)
        member = await app.get_chat_member(chat.id, "me")
    except RPCError as e:
        return False, (
            f"Couldn't access that chat ({e.MESSAGE if hasattr(e, 'MESSAGE') else e}). "
            "Check the @username / id, and make sure I've been added to the channel "
            "as an administrator."
        )
    if member.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
        return False, (
            f"I'm in “{chat.title}” but I'm not an administrator there. "
            "Promote me with the 'Post messages' right, then run /settarget again."
        )
    privileges = member.privileges
    if (
        member.status == enums.ChatMemberStatus.ADMINISTRATOR
        and privileges is not None
        and not privileges.can_post_messages
    ):
        return False, (
            f"I'm an admin in “{chat.title}” but I don't have the "
            "'Post messages' right. Enable it, then run /settarget again."
        )
    return True, ""


async def store_thumbnail(status_msg: Message, photo_msg: Message, app: Client) -> None:
    user_id = photo_msg.from_user.id if photo_msg.from_user else status_msg.chat.id
    cfg = user_cfg(user_id)
    os.makedirs(THUMB_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw.jpg")
        await photo_msg.download(file_name=raw)
        dest = os.path.join(THUMB_DIR, f"{status_msg.chat.id}.jpg")
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


# ----------------------------------------------------------------- app setup


def build_app() -> Client:
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    bot_token = os.environ.get("BOT_TOKEN")
    if not (api_id and api_hash and bot_token):
        raise SystemExit(
            "Set API_ID, API_HASH (from https://my.telegram.org) and BOT_TOKEN "
            "(from @BotFather) environment variables."
        )
    if not ffmpeg_available():
        log.warning("ffmpeg not found — thumbnail embedding will be skipped.")

    app = Client(
        "repost_bot_session",
        api_id=int(api_id),
        api_hash=api_hash,
        bot_token=bot_token,
        workdir=BASE_DIR,
        max_concurrent_transmissions=8,  # parallel chunks for large-file transfer speed
    )

    @app.on_message(filters.command(["start", "help"]) & filters.private)
    async def cmd_start(_, message: Message):
        await message.reply_text(HELP_TEXT)

    @app.on_message(filters.command("setkeywords") & filters.private)
    async def cmd_setkeywords(_, message: Message):
        cfg = user_cfg(message.from_user.id)
        raw = command_arg(message)
        keywords = [k.strip() for k in raw.split(",") if k.strip()]
        if not keywords:
            await message.reply_text("Usage: /setkeywords word1, word2, some phrase")
            return
        cfg["keywords"] = keywords
        save_config(CONFIG)
        await message.reply_text("Keywords to remove:\n" + "\n".join(f"• {k}" for k in keywords))

    @app.on_message(filters.command("clearkeywords") & filters.private)
    async def cmd_clearkeywords(_, message: Message):
        cfg = user_cfg(message.from_user.id)
        cfg["keywords"] = []
        save_config(CONFIG)
        await message.reply_text("Keyword list cleared.")

    @app.on_message(filters.command("setcaption") & filters.private)
    async def cmd_setcaption(_, message: Message):
        cfg = user_cfg(message.from_user.id)
        text = command_arg(message)
        if not text:
            await message.reply_text("Usage: /setcaption Your replacement text")
            return
        cfg["replacement"] = text
        save_config(CONFIG)
        await message.reply_text(f"Replacement text set:\n{text}")

    @app.on_message(filters.command("clearcaption") & filters.private)
    async def cmd_clearcaption(_, message: Message):
        cfg = user_cfg(message.from_user.id)
        cfg["replacement"] = ""
        save_config(CONFIG)
        await message.reply_text("Replacement text cleared.")

    @app.on_message(filters.command("setmode") & filters.private)
    async def cmd_setmode(_, message: Message):
        cfg = user_cfg(message.from_user.id)
        mode = command_arg(message).lower()
        if mode not in VALID_MODES:
            await message.reply_text(
                "Usage: /setmode append|prepend|replace\n"
                "append — cleaned caption, then your text\n"
                "prepend — your text, then cleaned caption\n"
                "replace — only your text"
            )
            return
        cfg["mode"] = mode
        save_config(CONFIG)
        await message.reply_text(f"Caption mode: {mode}")

    @app.on_message(filters.command("settarget") & filters.private)
    async def cmd_settarget(client: Client, message: Message):
        cfg = user_cfg(message.from_user.id)
        arg = command_arg(message)
        if not arg:
            await message.reply_text(
                "Usage: /settarget @channelusername or /settarget -1001234567890"
            )
            return

        m = re.match(r"(?:https?://)?t\.me/(.+)", arg)
        if m:
            arg = m.group(1).strip("/")
            if arg.startswith("+") or arg.startswith("joinchat"):
                await message.reply_text(
                    "That's a private invite link — bots can't join channels via invite "
                    "links. Add me to the channel as an admin yourself, then send "
                    "/settarget with the channel's @username, or (for private channels) "
                    "its numeric id like -1001234567890.\n"
                    "Tip: forward any post from the channel to @userinfobot to get the id."
                )
                return

        target = int(arg) if re.fullmatch(r"-?\d+", arg) else (arg if arg.startswith("@") else "@" + arg)

        ok, err = await verify_admin(client, target)
        if not ok:
            await message.reply_text(f"❌ {err}")
            return

        chat = await client.get_chat(target)
        cfg["target_chat_id"] = chat.id
        cfg["target_title"] = chat.title or str(target)
        save_config(CONFIG)
        await message.reply_text(
            f"✅ Target channel set: {cfg['target_title']} (id {chat.id}). "
            "I verified that I'm an admin and can post there."
        )

    @app.on_message(filters.command("setthumb") & filters.private)
    async def cmd_setthumb(client: Client, message: Message):
        reply = message.reply_to_message
        if reply and reply.photo:
            await store_thumbnail(message, reply, client)
            return
        AWAITING_THUMB.add(message.from_user.id)
        await message.reply_text("Send me the photo to use as the new video thumbnail.")

    @app.on_message(filters.command("clearthumb") & filters.private)
    async def cmd_clearthumb(_, message: Message):
        cfg = user_cfg(message.from_user.id)
        if cfg.get("thumb_path") and os.path.exists(cfg["thumb_path"]):
            os.remove(cfg["thumb_path"])
        cfg["thumb_path"] = None
        save_config(CONFIG)
        await message.reply_text("Thumbnail cleared — videos keep their own thumbnail.")

    @app.on_message(filters.command("status") & filters.private)
    async def cmd_status(_, message: Message):
        cfg = user_cfg(message.from_user.id)
        kw = ", ".join(cfg["keywords"]) or "(none)"
        await message.reply_text(
            f"Keywords to remove: {kw}\n"
            f"Replacement text: {cfg['replacement'] or '(none)'}\n"
            f"Caption mode: {cfg.get('mode', 'append')}\n"
            f"Target channel: {cfg['target_title'] or '(not set)'}\n"
            f"Custom thumbnail: {'set' if cfg.get('thumb_path') else '(none)'}\n"
            f"ffmpeg: {'available' if ffmpeg_available() else 'NOT FOUND (thumbnail embedding disabled)'}"
        )

    @app.on_message(
        filters.private & (filters.video | filters.photo | filters.document | filters.animation)
    )
    async def handle_media(client: Client, message: Message):
        if message.photo and (
            message.from_user.id in AWAITING_THUMB
            or (message.caption or "").strip().lower().startswith("/setthumb")
        ):
            AWAITING_THUMB.discard(message.from_user.id)
            await store_thumbnail(message, message, client)
            return

        # Enqueue synchronously (no awaits before this point past the thumbnail
        # check) so reposts happen strictly in the order files were forwarded —
        # a quick copy_message must not overtake a large video ahead of it.
        REPOST_QUEUE.put_nowait((client, message))
        if REPOST_QUEUE.qsize() > 1:
            await message.reply_text(
                f"🕐 Queued (position {REPOST_QUEUE.qsize()}) — posting in order."
            )
        ensure_worker()

    @app.on_message(filters.private & filters.text & filters.forwarded)
    async def handle_forwarded_text(client: Client, message: Message):
        # forwarded text posts go through the same FIFO queue as media
        REPOST_QUEUE.put_nowait((client, message))
        if REPOST_QUEUE.qsize() > 1:
            await message.reply_text(
                f"🕐 Queued (position {REPOST_QUEUE.qsize()}) — posting in order."
            )
        ensure_worker()

    @app.on_message(filters.private & filters.text & ~filters.forwarded & ~filters.command([
        "start", "help", "setkeywords", "clearkeywords", "setcaption", "clearcaption",
        "setmode", "settarget", "setthumb", "clearthumb", "status",
    ]))
    async def handle_other(_, message: Message):
        await message.reply_text(
            "Send me a video, photo, document or a forwarded text to repost, "
            "or /help for commands."
        )

    return app


async def process_repost(client: Client, message: Message) -> None:
    """Repost one forwarded media or text message to the user's target channel."""
    cfg = user_cfg(message.from_user.id)
    if not cfg["target_chat_id"]:
        await message.reply_text("No target channel configured. Use /settarget first.")
        return

    ok, err = await verify_admin(client, cfg["target_chat_id"])
    if not ok:
        await message.reply_text(f"❌ Can't post to {cfg['target_title']}: {err}")
        return

    if message.text:
        final_text = build_caption(message.text, cfg, limit=TEXT_LIMIT)
        if not final_text:
            await message.reply_text(
                "Nothing left to post after keyword removal — skipped."
            )
            return
        try:
            await client.send_message(chat_id=cfg["target_chat_id"], text=final_text)
            await message.reply_text(f"✅ Posted to {cfg['target_title']}.")
        except RPCError as e:
            await message.reply_text(f"❌ Telegram rejected the post: {e}")
        return

    final_caption = build_caption(message.caption or "", cfg)

    try:
        if message.video and cfg.get("thumb_path"):
            await repost_video_with_thumb(client, message, cfg, final_caption)
        else:
            await client.copy_message(
                chat_id=cfg["target_chat_id"],
                from_chat_id=message.chat.id,
                message_id=message.id,
                caption=final_caption,
            )
            await message.reply_text(f"✅ Posted to {cfg['target_title']}.")
    except RPCError as e:
        await message.reply_text(f"❌ Telegram rejected the post: {e}")


class ProgressReporter:
    """Throttled progress editor so long transfers show visible movement."""

    def __init__(self, note: Message, label: str):
        self.note = note
        self.label = label
        self._last_edit = 0.0
        self._last_pct = -1

    async def __call__(self, current: int, total: int) -> None:
        pct = int(current * 100 / total) if total else 0
        now = asyncio.get_running_loop().time()
        if pct == self._last_pct or (now - self._last_edit < 4 and pct < 100):
            return
        self._last_edit = now
        self._last_pct = pct
        mb = lambda n: n / 1024 / 1024
        try:
            await self.note.edit_text(
                f"⏳ {self.label}… {pct}% ({mb(current):.0f}/{mb(total):.0f} MB)"
            )
        except RPCError:
            pass  # ignore flood-wait/edit races, the transfer itself is unaffected


async def repost_video_with_thumb(
    client: Client, message: Message, cfg: dict, caption: str
) -> None:
    """Download video, embed the configured thumbnail without transcoding, upload."""
    video = message.video
    target = cfg["target_chat_id"]

    async def copy_without_thumb(reason: str) -> None:
        await client.copy_message(
            chat_id=target, from_chat_id=message.chat.id,
            message_id=message.id, caption=caption,
        )
        await message.reply_text(
            f"⚠️ Posted to {cfg['target_title']} with the original thumbnail: {reason}"
        )

    if video.file_size and video.file_size > MAX_FILE_SIZE:
        await copy_without_thumb(
            f"this {video.file_size / 1024 / 1024:.0f} MB video exceeds Telegram's "
            "own upload limit for thumbnail replacement."
        )
        return
    if not ffmpeg_available():
        await copy_without_thumb("ffmpeg is not installed on the bot's machine.")
        return

    note = await message.reply_text("⏳ Downloading… 0%")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DOWNLOAD_DIR) as tmp:
        ext = os.path.splitext(video.file_name or "")[1] or ".mp4"
        src = os.path.join(tmp, "in" + ext)
        out = os.path.join(tmp, "out.mp4")
        try:
            await message.download(file_name=src, progress=ProgressReporter(note, "Downloading"))
        except RPCError as e:
            await note.delete()
            await copy_without_thumb(f"Telegram wouldn't let me download the file ({e}).")
            return

        await note.edit_text("⏳ Embedding thumbnail…")
        upload_path = src
        loop = asyncio.get_running_loop()
        embedded = await loop.run_in_executor(None, attach_thumbnail, src, cfg["thumb_path"], out)
        if embedded:
            upload_path = out
        # embedding can fail on exotic containers; the send_video `thumb` argument
        # below still sets the visible Telegram thumbnail either way

        if os.path.getsize(upload_path) > MAX_FILE_SIZE:
            await note.delete()
            await copy_without_thumb("the result exceeds Telegram's upload limit.")
            return

        await client.send_video(
            chat_id=target,
            video=upload_path,
            caption=caption,
            thumb=cfg["thumb_path"],
            duration=video.duration or 0,
            width=video.width or 0,
            height=video.height or 0,
            supports_streaming=True,
            progress=ProgressReporter(note, "Uploading"),
        )
    await note.delete()
    extra = "" if embedded else " (thumbnail set via Telegram; embedding in the file itself failed)"
    await message.reply_text(f"✅ Posted to {cfg['target_title']} with the new thumbnail.{extra}")


def main() -> None:
    app = build_app()
    log.info("Bot starting…")
    app.run()


if __name__ == "__main__":
    main()
