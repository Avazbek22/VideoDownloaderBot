# VideoDownloaderBot

## A **self‑hosted Telegram bot** that downloads media from a link using **yt-dlp**.

> Paste a link → bot fetches info instantly → you choose how to receive it.
>
> **Video** (Telegram video) • **Document** (original file) • **Audio** (MP3)

**Hard size limit:** **50 MB** (video/document). Audio is available **only if it can fit into 50 MB**.

**Clean UX:** a single status message updates during download/upload, then **disappears on success** (only the media stays).

Works best on **Ubuntu 22.04 / 24.04**. **Docker is recommended.**

---

## Why this bot exists

Most “download bots” are either public (unstable / rate-limited / banned), or self-hosted but painful to set up.

**VideoDownloaderBot** is intentionally:

* **Self-hosted first** (VPS / home server)
* **Strict & predictable** (50 MB limit, no “maybe it will fit” downloads)
* **No nonsense setup** (one command installer)
* **Nice UX in Telegram** (buttons, progress, cancel)

---

## Features

✅ Supports many websites (YouTube, TikTok, X/Twitter, Reddit, and more — whatever yt-dlp supports)

✅ Inline buttons: **Video / Document / Audio**

✅ Progress feedback (**download + upload**) with message edits (Telegram-friendly throttling)

✅ **Queue + workers** (controlled concurrency)

✅ **Cancel** button during the process (download *and* upload)

✅ Safe filenames:

* uses video title
* removes hashtags
* sanitizes forbidden characters
* avoids weird trailing dots/spaces

✅ **Strict 50 MB pre-check**:

* if it won’t fit → bot tells you **before downloading**
* if it can’t reliably estimate size → bot refuses (by design)

✅ Designed for **private self-hosting** (not a public “open” downloader service)

---

## How it works (in one minute)

1. You send a URL.

2. Bot calls yt-dlp **metadata-only** (no download yet).

3. Bot builds a **video plan**:

* tries best **progressive MP4** (video+audio in one file)
* otherwise uses best **MP4 video + M4A audio** (merged)

4. Bot tries to **prove** final size is ≤ 50 MB:

* uses `filesize` / `filesize_approx` when available
* if missing, tries a **Range probe** on direct media URLs (`Content-Range`) to get real byte size

5. If size can be proven and fits:

* it offers **Video / Document** (and Audio if available)

6. For **Audio (MP3)**:

* uses duration to pick the **highest MP3 bitrate** that will safely fit (with headroom)

7. During download/upload:

* status message updates with progress
* you can cancel anytime

8. On success:

* status message is deleted
* only the final media remains in chat

---

## Quick start (recommended): one‑line installer

Run on your Ubuntu server (SSH):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Avazbek22/VideoDownloaderBot/master/install.sh)
```

The installer will:

* Install system deps (**Python**, **venv**, **git**, **ffmpeg**, certificates)
* Ask only for your **Telegram bot token**
* Ask if you want **Docker install** (recommended)
* Start the bot and show commands to view logs / stop

> No Python knowledge required. One command on a fresh Ubuntu VPS.

---

## After installation

### If installed with Docker

Go to the install folder printed by the script (default: `~/VideoDownloaderBot`) and use:

```bash
cd ~/VideoDownloaderBot

# logs
# (works with either docker compose v2 or docker-compose v1)
docker compose logs -f --tail=200 || docker-compose logs -f --tail=200

# restart
(docker compose restart || docker-compose restart)

# stop + remove container
(docker compose down || docker-compose down)

# start
(docker compose up -d || docker-compose up -d)
```

### If installed without Docker (system service)

```bash
sudo systemctl status videodownloaderbot
sudo systemctl restart videodownloaderbot
sudo journalctl -u videodownloaderbot -f --no-pager
```

---

## Bot usage

1. Open your bot in Telegram
2. Send any supported URL
3. You’ll see the title + buttons:

* **Download as Video**
* **Download as Document** (original file)
* **Download as Audio (MP3)** (only when it can fit)

During work you can press **Cancel**.

### What you’ll see in chat

* “Getting info…” (short)
* “Choose download method…” + size/quality info
* Status message updates:

  * ⬇️ Downloading…
  * ⬆️ Sending video/document/audio…
* On success: status message disappears, only media stays

---

## Commands

### `/start` and `/help`

Shows a short help message and the current upload limit.

### `/custom <url>` (kept as-is)

Fetches formats and lets you pick one.

> Note: after you pick a format, the bot returns to the same “choice UI” flow.

---

## Configuration

### The only required value

* `BOT_TOKEN` — Telegram bot token from BotFather

### Recommended config (installer)

The installer creates a local `.env` (not meant to be committed):

```env
BOT_TOKEN=123456:ABCDEF...
OUTPUT_FOLDER=/tmp/yt-dlp-telegram
```

And it generates `config.py` that reads from environment variables.

### Optional

* `OUTPUT_FOLDER` — where temporary files are stored (default: `/tmp/yt-dlp-telegram`)

### Hardcoded defaults (by design)

* `logs = None`
* `max_filesize = 50 MB`

> If you really want logging to a Telegram chat/channel, set `logs` manually in `config.py`.

---

## Manual installation (no installer)

If you prefer doing everything yourself:

```bash
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
  ca-certificates git python3 python3-venv python3-pip ffmpeg

git clone https://github.com/Avazbek22/VideoDownloaderBot.git
cd VideoDownloaderBot

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# create .env
cat > .env <<'EOF'
BOT_TOKEN=PUT_YOUR_TOKEN_HERE
OUTPUT_FOLDER=/tmp/yt-dlp-telegram
EOF

# run
set -a; source .env; set +a
python -u main.py
```

---

## Requirements

* Ubuntu 22.04 / 24.04 (recommended)
* Python 3.11+ (Docker uses 3.11-slim)
* ffmpeg
* Telegram bot token

Python dependencies (see `requirements.txt`):

* `yt-dlp`
* `pyTelegramBotAPI`
* `requests-toolbelt` (upload progress)

---

## Limitations (intentional)

### 50 MB is strict

This project is intentionally strict and predictable.
If the bot expects a file won’t fit, it stops early and tells you **before downloading**.

### “Unknown size” videos are refused

If the bot can’t reliably determine the final size, it refuses video/document download.
This avoids wasting time, bandwidth, and disk space on videos that will fail at upload.

### Telegram compression behavior

Telegram compression depends on client/platform/codec/bitrate.
Sometimes “Video” looks nearly identical to “Document”. Document is always **as-is**.

---

## FAQ

### Why is there a 50 MB limit?

Because this project is intentionally strict and predictable. If the bot expects that a file won’t fit, it stops early and tells you before downloading.

### Is this a public bot?

No. This repository is intended for **self-hosted private use**.

### Why does Docker build show “Debian … trixie/bookworm …” packages?

That’s inside the container image (base `python:*‑slim` images are Debian-based). Your Ubuntu host is not being “converted”.

### YouTube prints warnings about JavaScript runtime

yt-dlp may warn about missing JS runtime. The bot can still work, but some formats might be missing. You can extend the Dockerfile later to include a runtime if you want.

### I ran `install.sh` on Windows PowerShell and `chmod` is not found

`install.sh` is a Linux script. Run it on Ubuntu/WSL/your VPS via Bash.

---

## Troubleshooting

### “Request expired. Send the link again.”

The button UI has a TTL to prevent memory leaks. Just send the link again.

### “This video is too large…” even before download

That’s the strict pre-check working as intended.
Try a shorter video, or use Audio if available.

### Docker permission denied

If you installed Docker and your user can’t access the daemon, re-login or run:

```bash
sudo usermod -aG docker $USER
```

Then reconnect your SSH session.

---

## Project structure

```
.
├── main.py              # Bot logic (queue, progress, pre-check, upload)
├── install.sh           # One-line installer (Docker or system service)
├── requirements.txt     # Python deps
├── example.config.py    # Example (installer generates config.py dynamically)
├── LICENSE
└── README.md
```

> Docker files (`Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh`) are generated by `install.sh`.

---

## Contributing

PRs are welcome.
If you want new features (better format picking, larger limits via local Bot API/MTProto, etc.) open an issue.

Ideas that fit this project’s philosophy:

* Better format selection *without* quality squeezing
* Better website-specific fallbacks
* Optional JS runtime layer in Docker
* More robust size probing for edge sites

---

## License

MIT License

Copyright (c) 2025 Avazbek22

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
