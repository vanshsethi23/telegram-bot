# Telegram Media Repost Bot

Forward a video/photo/document to this bot and it automatically:

1. strips your configured keywords from the caption (case-insensitive),
2. adds your replacement text (append / prepend / replace),
3. optionally replaces the video thumbnail (ffmpeg stream-copy — the video is **not** re-encoded),
4. posts the result to your target channel. No manual step after forwarding.

## Setup

This bot uses Pyrogram (MTProto) instead of the plain Bot API so file transfers
aren't capped at the Bot API's 20 MB download / 50 MB upload limits — files up
to Telegram's own ~2 GB limit work directly. This means you need **both** a bot
token and an `api_id`/`api_hash` pair.

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Get an `api_id` and `api_hash` from <https://my.telegram.org> → **API
   development tools** (free, just needs your phone number — this identifies
   the *application*, not a user account; the bot still logs in with its own
   token).
3. Install dependencies (Python 3.10+):

   ```bash
   pip install -r requirements.txt
   ```

   If installing fails with a `pyaes` wheel-build error, retry with:
   ```bash
   SETUPTOOLS_USE_DISTUTILS=stdlib pip install -r requirements.txt
   ```

4. Install ffmpeg (needed only for thumbnail replacement):

   ```bash
   sudo apt install ffmpeg      # Debian/Ubuntu
   brew install ffmpeg          # macOS
   ```

5. Add the bot to your target channel as an **administrator** with the
   **Post messages** right.

6. Run it:

   ```bash
   API_ID=12345 API_HASH=abcdef0123456789abcdef0123456789 BOT_TOKEN=123456:ABC-your-token python3 bot.py
   ```

   First run creates a `repost_bot_session.session` file next to `bot.py` —
   keep it, it lets the bot reconnect without re-authenticating.

## Configure (in a private chat with the bot)

| Command | What it does |
|---|---|
| `/setkeywords word1, word2, some phrase` | Comma-separated keywords to strip from captions |
| `/setcaption Your text here` | Replacement text to add to every caption |
| `/setmode append\|prepend\|replace` | How the replacement text is combined with the cleaned caption (default `append`) |
| `/settarget @mychannel` or `/settarget -1001234567890` | Target channel — the bot verifies it is admin and can post |
| `/setthumb` | Then send a photo — used as thumbnail for all subsequent videos |
| `/clearthumb`, `/clearkeywords`, `/clearcaption` | Clear the respective setting |
| `/status` | Show current configuration |

Then just **forward media to the bot** — it reposts to the channel automatically.

Config is persisted in `config.json` (and `thumbnails/`) next to `bot.py`,
so it survives restarts.

## Running 24/7 on a server

A systemd unit template is included in `deploy/telegram-repost-bot.service` —
it auto-starts the bot on boot and restarts it on crashes. On a fresh Ubuntu
server:

```bash
sudo apt update && sudo apt install -y python3-venv ffmpeg git
git clone https://github.com/vanshsethi23/telegram-bot.git ~/telegram-bot
cd ~/telegram-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
sudo cp deploy/telegram-repost-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/telegram-repost-bot.service   # fill in API_ID/API_HASH/BOT_TOKEN
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-repost-bot
journalctl -u telegram-repost-bot -f    # watch logs
```

Only run ONE copy of the bot at a time (server OR laptop, never both) —
two processes sharing a bot token fight over updates and both misbehave.

## Notes & limits

- **Private channels:** bots can't join via invite links. Add the bot as admin
  yourself, then use the channel's numeric id (`-100...`) with `/settarget`
  (forward a post from the channel to @userinfobot to find the id).
- **Thumbnail replacement** requires downloading and re-uploading the video.
  Since the bot talks MTProto directly (not the restricted Bot API), this works
  up to Telegram's own ~2 GB upload ceiling. Larger videos are reposted with
  their original thumbnail and the bot tells you why.
  Without a custom thumbnail there is no size limit concern at all — the bot
  uses `copy_message`, which never downloads the file.
- Thumbnails are auto-converted to Telegram's requirements (JPEG, ≤ 320 px).
- Captions are truncated to Telegram's 1024-character limit.
