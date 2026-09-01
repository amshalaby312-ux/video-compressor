# Telegram Video/PDF Compressor Bot

Send the bot a video or a PDF — it downloads it, compresses it, and sends
the compressed file back with size stats.

- **Video** → compressed with `ffmpeg` (H.264, CRF-based, adjustable quality/resolution)
- **PDF** → compressed with `Ghostscript` (image downsampling — this is where most PDF size comes from), with a lossless `pikepdf` fallback if Ghostscript isn't installed
- `/settings` lets each user pick a compression level via inline buttons
- Shows a live progress percentage while compressing video
- Reports original size, compressed size, and % saved

## 1. Install system dependencies

```bash
# Debian/Ubuntu
sudo apt update && sudo apt install -y ffmpeg ghostscript

# macOS (Homebrew)
brew install ffmpeg ghostscript

# Windows
# Install ffmpeg: https://ffmpeg.org/download.html (add to PATH)
# Install Ghostscript: https://ghostscript.com/releases/gsdnld.html (add to PATH, binary is "gswin64c" — see note below)
```

> **Windows note:** Ghostscript's binary is `gswin64c.exe`, not `gs`. Either add a `gs.bat` shim to PATH that calls `gswin64c`, or edit `check_binary`/the `gs` command in `bot.py` to use `gswin64c`.

## 2. Install Python dependencies

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Get a bot token

Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`, and follow the
prompts. You'll get a token like `123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ`.

## 4. Run it

```bash
export BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
python bot.py
```

Now open a chat with your bot on Telegram and send it a video or PDF.

## Telegram file size limits (important)

The **regular Bot API** (the default, and what this bot uses out of the box)
restricts bots to:
- Downloading files **up to 20 MB**
- Uploading files **up to 50 MB**

These are Telegram-side limits, not something this bot can bypass on its own.

### Raising the limit: run your own local Bot API server

Telegram provides an open-source [Bot API server](https://github.com/tdlib/telegram-bot-api)
you can self-host, which raises these limits to **~2000 MB**. Rough steps:

```bash
# Build/run (see the repo for full instructions and Docker option)
docker run -d -p 8081:8081 \
  -e TELEGRAM_API_ID=<your_api_id> \
  -e TELEGRAM_API_HASH=<your_api_hash> \
  -v telegram-bot-api-data:/var/lib/telegram-bot-api \
  aiogram/telegram-bot-api:latest
```

You get `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` from https://my.telegram.org.

Then point this bot at your local server:

```bash
export LOCAL_BOT_API_URL="http://localhost:8081"
export BOT_TOKEN="..."
python bot.py
```

The script auto-detects `LOCAL_BOT_API_URL` and raises its internal size
limits accordingly.

## Adjusting compression presets

Edit the `VIDEO_PRESETS` and `PDF_PRESETS` dicts at the top of `bot.py`:

- **Video** — `crf` (18=near-lossless … 34=very compressed), `scale` (e.g. `-2:720`
  to cap height at 720p), and `speed` (ffmpeg's `-preset`, trades encode time for
  compression efficiency: `ultrafast` … `veryslow`).
- **PDF** — Ghostscript's `-dPDFSETTINGS` presets: `/prepress` (highest quality),
  `/printer`, `/ebook` (good default), `/screen` (smallest, 72dpi images).

## Deploying on Railway

This repo includes a `Dockerfile` and `railway.json` so Railway builds it as a
Docker service (needed because `ffmpeg`/`ghostscript` aren't in Railway's default
Python buildpack).

1. Push this folder to a GitHub repo (or use `railway up` from the CLI directly).
2. In Railway: **New Project → Deploy from GitHub repo**, pick this repo.
   Railway will detect the `Dockerfile` and build from it automatically.
3. Go to your service's **Variables** tab and add:
   - `BOT_TOKEN` = your token from @BotFather
4. Deploy. Check the **Deploy Logs** — you should see `Bot starting...`.

**Important:** this bot polls Telegram for updates rather than running a web
server, so it doesn't listen on a port. Railway's default health check expects
an HTTP response on a port and can mark the deploy "unhealthy" even though the
bot is working fine. `railway.json` in this repo already sets a restart policy
without an HTTP health check, so this should work out of the box — but if you
see Railway flagging it as unhealthy, go to **Settings → Healthcheck** on the
service and make sure no healthcheck path is set (or disable it).

**Ephemeral filesystem:** Railway containers don't persist disk storage between
deploys/restarts. That's fine here — the bot only ever uses temp folders for the
duration of a single compression job and deletes them immediately after. No
persistent volume needed.

**File size limits still apply on Railway.** The 20 MB download / 50 MB upload
caps are enforced by Telegram's regular Bot API regardless of where the bot
runs. Self-hosting a local Bot API server to raise this (see above) is possible
on Railway too — it would mean deploying `telegram-bot-api` as a second Railway
service and pointing `LOCAL_BOT_API_URL` at its internal Railway URL — but it's
an extra moving part, so only bother if you actually need bigger files.

**Costs / sleeping:** Railway's free tier has monthly usage limits and can sleep
inactive services depending on your plan. A polling bot needs to stay running
continuously to receive messages, so keep an eye on your plan's always-on
allowance.

## Running it 24/7 (non-Docker hosts)

For a simple always-on setup outside Railway, use `systemd` (Linux) or a
process manager like `pm2` or `supervisord`. Example `systemd` unit:

```ini
[Unit]
Description=Telegram Compressor Bot
After=network.target

[Service]
Environment=BOT_TOKEN=your_token_here
WorkingDirectory=/path/to/tg_compressor_bot
ExecStart=/path/to/venv/bin/python bot.py
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

## Notes & limitations

- In-memory settings: per-user preferences reset if the bot restarts. Swap
  `USER_SETTINGS` for a small SQLite table if you want persistence.
- Only one file is processed at a time per message (no batch/album handling).
- Video compression re-encodes both video and audio, so it works on any
  input codec ffmpeg supports — but it's not instant; a few minutes of 1080p
  footage can take a minute or more to re-encode depending on your CPU and
  chosen preset speed.
- Everything runs locally in temp folders that are cleaned up after each job.
