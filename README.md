# VideoDownloaderBot

A self‑hosted Telegram bot that downloads media from a link using **yt-dlp**.

* Paste a link → bot fetches info instantly → you choose how to receive it.
* **Download as Video** (Telegram video) or **Download as Document** (original file).
* **Download as Audio (MP3)**.
* **Hard size limit: 50 MB** (video/document). Audio is allowed only if it’s within 50 MB.
* Clean UX: status message updates during download/upload, then disappears on success.

> Works best on **Ubuntu 22.04 / 24.04**. Docker is recommended.

---

## Features

* ✅ Supports many websites (YouTube, TikTok, X/Twitter, Reddit, and more — whatever yt-dlp supports)
* ✅ Inline buttons: **Video / Document / Audio**
* ✅ Progress feedback (download + upload)
* ✅ Queue + workers (controlled concurrency)
* ✅ Cancel button during the process
* ✅ Safe filename: uses video title, removes hashtags, sanitizes forbidden characters
* ✅ Strict 50 MB pre-check: if it won’t fit → bot tells you and **does not download**

---

## Quick start (recommended): one‑line installer

Run on your Ubuntu server (SSH):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Avazbek22/VideoDownloaderBot/master/install.sh)
```

The installer will:

1. Install system deps (Python, venv, git, ffmpeg, certificates)
2. Ask **only** for your Telegram bot token
3. Ask if you want **Docker** install
4. Start the bot and show the commands to view logs / stop

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
   * **Download as Document**
   * **Download as Audio (MP3)**
4. During work you can press **Cancel**.

---

## Configuration

Only one thing is required:

* `BOT_TOKEN` — Telegram bot token from BotFather

The installer writes it to a local `.env` (not meant to be committed).

Optional:

* `OUTPUT_FOLDER` — where temporary files are stored (default: `/tmp/yt-dlp-telegram`)

Hardcoded defaults (by design):

* `logs = None`
* `max_filesize = 50 MB`

---

## FAQ

### Why is there a 50 MB limit?

Because this project is intentionally strict and predictable. If the bot expects that a file won’t fit, it stops early and tells you **before downloading**.

### Why does Docker build show “Debian … trixie/bookworm …” packages?

That’s inside the container image (base `python:*‑slim` images are Debian-based). Your Ubuntu host is not being “converted”.

### Telegram didn’t compress “Video” vs “Document”. Why?

Telegram compression behavior depends on client/platform/codec/bitrate and sometimes it keeps the file almost identical. Document is always “as-is”.

### YouTube prints warnings about JavaScript runtime.

yt-dlp may warn about missing JS runtime. The bot can still work, but some formats might be missing. You can extend the Dockerfile later to include a runtime if you want.

### I ran install.sh on Windows PowerShell and `chmod` is not found.

`install.sh` is a Linux script. Run it on Ubuntu/WSL/your VPS via Bash.

---

## Contributing

PRs are welcome. If you want new features (better format picking, larger limits via local Bot API/MTProto, etc.) open an issue.

---

## License (MIT)

MIT License

Copyright (c) 2025 Avazbek22

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
