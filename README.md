# Telegram Media Repost Bot

Forward a video/photo/document to this bot and it automatically:

1. strips your configured keywords from the caption (case-insensitive),
2. adds your replacement text (append / prepend / replace),
3. optionally replaces the video thumbnail (ffmpeg stream-copy — the video is **not** re-encoded),
4. posts the result to your target channel. No manual step after forwarding.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Install dependencies (Python 3.10+):

   ```bash
   pip install -r requirements.txt
   ```

3. Install ffmpeg (needed only for thumbnail replacement):

   ```bash
   sudo apt install ffmpeg      # Debian/Ubuntu
   brew install ffmpeg          # macOS
   ```

4. Add the bot to your target channel as an **administrator** with the
   **Post messages** right.

5. Run it:

   ```bash
   BOT_TOKEN=123456:ABC-your-token python3 bot.py
   ```

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

## Notes & limits

- **Private channels:** bots can't join via invite links. Add the bot as admin
  yourself, then use the channel's numeric id (`-100...`) with `/settarget`
  (forward a post from the channel to @userinfobot to find the id).
- **Thumbnail replacement** requires downloading and re-uploading the video, so
  Bot API limits apply: download ≤ 20 MB, upload ≤ 50 MB. Larger videos are
  reposted with their original thumbnail and the bot tells you why.
  Without a custom thumbnail there is no size limit at all — the bot uses
  `copyMessage`, which never downloads the file.
- Thumbnails are auto-converted to Telegram's requirements (JPEG, ≤ 320 px).
- Captions are truncated to Telegram's 1024-character limit.
