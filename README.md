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

3. Bot builds a best-first list of **video candidates**:

* ranks Telegram-compatible H.264 MP4 by compatibility and quality
* supports both progressive MP4 and MP4 video + M4A audio without automatically preferring progressive
* handles deduplicated Instagram direct MP4 candidates whose codec metadata is missing

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
bash <(curl -fsSL https://raw.githubusercontent.com/Avazbek22/VideoDownloaderBot/main/install.sh)
```

The installer will:

* Install **Git**, **Docker**, Compose, and `flock` when needed
* Preserve an existing `.env`, `data/`, and `logs/`
* Ask for **BOT_TOKEN** only when it is not configured
* Build and validate the production image before replacing the container
* Enable automatic deployment from `origin/main` and nightly yt-dlp image updates

> No Python knowledge required. One command on a fresh Ubuntu VPS.

---

## After installation

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

Runtime logs are also written to `logs/bot.log`. Downloads use per-job directories under `data/downloads/`; both directories survive deployment and rollback.

### Automatic deployment and rollback

`videodownloaderbot-deploy.timer` checks `origin/main` every two minutes. Runtime changes are built and tested in a new image; documentation-only commits update the checkout without rebuilding the container. Before replacement, deployment verifies Python imports, ffmpeg, Node, yt-dlp, and Telegram `getMe`.

If build, preflight, container startup, or health stabilization fails, the previous Git commit and Docker image are restored. Changed systemd unit files are refreshed transactionally and restored too if deployment fails. The failed SHA is stored in `data/.failed-deploy-sha` and is not retried until a newer commit arrives. Daily deployment logs are stored in `logs/deploy-YYYY-MM-DD.log` for 60 days.

`videodownloaderbot-yt-dlp-update.timer` performs a nightly image rebuild using `YTDLP_CACHEBUST`. It never runs `pip install` inside the running container, does not pull a new base image, and does not restart the bot when yt-dlp is already current. A changed update rolls back to the previous image if validation or startup fails. Updater logs are stored in `logs/updater-YYYY-MM-DD.log`.

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

The installer creates `.env` from `.env-example` only when it does not exist:

```env
BOT_TOKEN=123456:ABCDEF...
OUTPUT_FOLDER=/app/data/downloads
LOGS_DIR=/app/logs
```

Operational settings are read through the validated `Settings` dataclass:

* `LOGS_CHAT_ID` — optional operator chat for critical failures
* `OUTPUT_FOLDER` — temporary per-job directories
* `LOGS_DIR` — rotating application logs
* `MAX_FILESIZE` — strict upload limit; defaults to `52428800` bytes (50 MiB)
* `WORKERS` — download workers; default `2`
* `MAX_QUEUE` — bounded queue size; default `200`
* `UPLOAD_WORKERS` — concurrent Telegram uploads; default `2`
* `JOB_TIMEOUT_SECONDS` — whole-job deadline; default `900`
* `PENDING_TTL_SECONDS` — button request lifetime; default `600`
* `YTDLP_CONCURRENT_FRAGMENTS` — segmented download concurrency; default `4`
* `YTDLP_JS_RUNTIMES` — yt-dlp JavaScript runtimes; default `node`
* `YTDLP_REMOTE_COMPONENTS` — optional yt-dlp remote components; empty disables them
* `YTDLP_YOUTUBE_PLAYER_CLIENTS` — ordered metadata fallback clients; default `default,android,ios`
* `YTDLP_INSTAGRAM_IMPERSONATE` — optional Instagram impersonation target
* `YTDLP_INSTAGRAM_RETRIES`, `YTDLP_INSTAGRAM_FRAGMENT_RETRIES`, `YTDLP_INSTAGRAM_SOCKET_TIMEOUT` — bounded Instagram retry/timeouts
* `METADATA_WORKERS` — maximum concurrent metadata operations; default `2`
* `METADATA_TIMEOUT_SECONDS` — metadata-stage timeout; default `60`
* `COOKIES_FILE` — optional cookies file, normally `/app/data/cookies.txt`
* `LOG_LEVEL` — `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`

---

## Manual installation (no installer)

If you prefer doing everything yourself:

```bash
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
  ca-certificates git python3 python3-venv python3-pip ffmpeg
# Install Node.js 22 or newer for current yt-dlp YouTube support.

git clone https://github.com/Avazbek22/VideoDownloaderBot.git
cd VideoDownloaderBot

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# create .env
cat > .env <<'EOF'
BOT_TOKEN=PUT_YOUR_TOKEN_HERE
OUTPUT_FOLDER=./data/downloads
LOGS_DIR=./logs
EOF

# run
python -u main.py
```

### Manual Docker start

The image runs as UID/GID `10001`, so prepare the two writable bind mounts before starting it:

```bash
cp .env-example .env
# Set BOT_TOKEN in .env, then:
mkdir -p data logs
sudo chown -R 10001:10001 data logs
chmod 600 .env
docker compose build
docker compose up -d
docker compose ps
```

The container root filesystem is read-only. Only `data/`, `logs/`, and the in-memory `/tmp` filesystem are writable; Docker health is based on a fresh `/tmp/videodownloaderbot.healthy` heartbeat.

---

## Requirements

* Ubuntu 22.04 / 24.04 (recommended)
* Python 3.11+ (Docker uses 3.12-slim)
* ffmpeg
* Node.js 22+
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

The production image installs a checksum-verified Node.js runtime. Check it with `docker compose exec videodownloaderbot node --version`. Manual installations require Node.js 22 or newer.

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
├── app/                     # Settings, planner, URL security, logging, helpers
├── scripts/                 # Entrypoint, deploy, yt-dlp updater, systemd units
├── tests/                   # Python and rollback shell tests
├── main.py                  # Existing Telegram UX and application lifecycle
├── Dockerfile
├── docker-compose.yml
├── install.sh               # Idempotent production installer
├── .env-example
├── requirements.txt
├── LICENSE
└── README.md
```

---

## Contributing

PRs are welcome.
If you want new features (better format picking, larger limits via local Bot API/MTProto, etc.) open an issue.

Ideas that fit this project’s philosophy:

* Better format selection *without* quality squeezing
* Better website-specific fallbacks
* Better website-specific runtime compatibility
* More robust size probing for edge sites

---

## License

MIT License

Copyright (c) 2025 Avazbek22

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
