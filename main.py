from urllib.parse import urlparse
import datetime
import telebot
import config
import yt_dlp
import os
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from telebot import types
from telebot.util import quick_markup
import time
import threading
import queue
import uuid
from typing import Optional, Dict, Any, Tuple, List

from app.http_utils import requests_session_with_retries as _requests_session_with_retries
from app.download_utils import (
    calc_download_progress as _calc_download_progress,
    find_downloaded_file as _find_downloaded_file_impl,
    find_file_by_prefix as _find_file_by_prefix_impl,
    render_status as _render_status,
)
from app.planner import (
    apply_youtube_runtime_opts as _apply_youtube_runtime_opts,
    apply_probe_if_needed as _apply_probe_if_needed,
    build_audio_plan_mp3 as _build_audio_plan_mp3,
    build_video_plan_no_squeeze as _build_video_plan_no_squeeze,
    get_video_meta as _get_video_meta,
    is_youtube_url as _is_youtube_url,
)
from app.text_utils import (
    extract_first_url as _extract_first_url,
    fmt_bytes as _fmt_bytes,
    sanitize_filename_base as _sanitize_filename_base,
    strip_hashtags as _strip_hashtags,
    youtube_url_validation,
)


# =========================
# Telegram bot init
# =========================
bot = telebot.TeleBot(config.token, threaded=True)

# Edit throttling (avoid Telegram flood limits)
EDIT_INTERVAL_SEC = 1.8

# Parallel jobs: how many downloads/uploads can run simultaneously
WORKERS = 2

# TTL for pending "choice" requests to avoid memory leaks
PENDING_TTL_SEC = 10 * 60

# Telegram Bot API upload limit (you keep it in config)
MAX_SEND_BYTES = int(getattr(config, "max_filesize", 50_000_000))

# yt-dlp optimization for segmented streams (HLS/DASH)
YTDLP_CONCURRENT_FRAGMENTS = 4
# YouTube JS challenge runtime settings (stable defaults, no account/cookies required)
YTDLP_JS_RUNTIMES = (os.getenv("YTDLP_JS_RUNTIMES") or "node").strip() or "node"
YTDLP_REMOTE_COMPONENTS = (os.getenv("YTDLP_REMOTE_COMPONENTS") or "ejs:github").strip() or "ejs:github"


# =========================
# Global state
# =========================
bot_lock = threading.RLock()

last_edited: Dict[str, datetime.datetime] = {}
last_text: Dict[str, str] = {}

pending_requests: Dict[str, Dict[str, Any]] = {}
jobs_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()

# Cancel support
cancel_events: Dict[str, threading.Event] = {}
active_jobs: Dict[str, Dict[str, Any]] = {}


# =========================
# Helpers (safe bot calls)
# =========================
def _bot_call(fn, *args, **kwargs):
    with bot_lock:
        return fn(*args, **kwargs)


def _safe_delete(chat_id: int, message_id: int) -> None:
    try:
        _bot_call(bot.delete_message, chat_id, message_id)
    except Exception:
        pass


def _safe_edit(chat_id: int, message_id: int, text: str, reply_markup=None, force: bool = False) -> None:
    key = f"{chat_id}-{message_id}"
    now = datetime.datetime.now()

    if not force:
        last = last_edited.get(key)
        if last is not None and (now - last).total_seconds() < EDIT_INTERVAL_SEC:
            return
        if last_text.get(key) == text:
            return

    try:
        _bot_call(
            bot.edit_message_text,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
        last_edited[key] = now
        last_text[key] = text
    except Exception:
        pass


def _safe_send_message(chat_id: int, text: str, reply_to_message_id: Optional[int] = None, reply_markup=None):
    try:
        return _bot_call(
            bot.send_message,
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    except Exception:
        return None


def _safe_answer_callback(call_id: str, text: str = "") -> None:
    try:
        _bot_call(bot.answer_callback_query, call_id, text=text)
    except Exception:
        pass


# =========================
# Cancel UI
# =========================
def _cancel_markup(job_id: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("Cancel", callback_data=f"cnl|{job_id}"))
    return kb


def _is_cancelled(job_id: str) -> bool:
    ev = cancel_events.get(job_id)
    return bool(ev and ev.is_set())


# =========================
# Upload via Bot API with progress + cancel
# =========================
def _send_via_bot_api_with_progress(
    job_id: str,
    chat_id: int,
    reply_to_message_id: int,
    status_message_id: int,
    title: str,
    method_name: str,
    file_field_name: str,
    file_path: str,
    send_filename: str,
    stage_label: str,
    extra_params: Dict[str, Any]
) -> None:
    api_url = f"https://api.telegram.org/bot{config.token}/{method_name}"
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    def render_upload(pct: Optional[int], sent: Optional[int], total_len: Optional[int]) -> str:
        line = f"Status: ⬆️ {stage_label}"
        if pct is not None:
            pct = max(0, min(100, int(pct)))
            line += f" {pct}%"
        if isinstance(sent, int) and isinstance(total_len, int) and total_len > 0:
            line += f"\n{_fmt_bytes(sent)} / {_fmt_bytes(total_len)}"
        return f"{title}\n\n{line}"

    _safe_edit(chat_id, status_message_id, render_upload(0, 0, file_size), reply_markup=_cancel_markup(job_id), force=True)

    if _is_cancelled(job_id):
        raise RuntimeError("Cancelled by user")

    with open(file_path, "rb") as f:
        fields = {
            "chat_id": str(chat_id),
            "reply_to_message_id": str(reply_to_message_id),
            **{k: str(v) for k, v in extra_params.items() if v is not None},
            file_field_name: (send_filename, f),
        }

        encoder = MultipartEncoder(fields=fields)

        def _cb(monitor: MultipartEncoderMonitor):
            if _is_cancelled(job_id):
                raise RuntimeError("Cancelled by user")

            total_len = monitor.len
            sent = monitor.bytes_read
            pct = int((sent * 100) / total_len) if total_len else None
            _safe_edit(chat_id, status_message_id, render_upload(pct, sent, total_len), reply_markup=_cancel_markup(job_id))

        monitor = MultipartEncoderMonitor(encoder, _cb)

        session = _requests_session_with_retries()
        try:
            resp = session.post(
                api_url,
                data=monitor,
                headers={"Content-Type": monitor.content_type},
                timeout=(20, 60 * 30),
            )
        finally:
            try:
                session.close()
            except Exception:
                pass

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"Telegram API error: HTTP {resp.status_code}")

    if not data.get("ok"):
        desc = data.get("description", "Unknown error")
        raise RuntimeError(f"Telegram API error: {desc}")

    _safe_edit(chat_id, status_message_id, render_upload(100, file_size, file_size), reply_markup=_cancel_markup(job_id), force=True)


# =========================
# Downloaded file discovery
# =========================
def _find_file_by_prefix(prefix: str, prefer_ext: Optional[str] = None) -> Optional[str]:
    return _find_file_by_prefix_impl(config.output_folder, prefix, prefer_ext=prefer_ext)


def _find_downloaded_file(info: Dict[str, Any], fallback_prefix: str, prefer_ext: Optional[str] = None) -> Optional[str]:
    return _find_downloaded_file_impl(info, config.output_folder, fallback_prefix, prefer_ext=prefer_ext)


# =========================
# Worker: download + send
# =========================
def _download_and_send(job: Dict[str, Any]) -> None:
    job_id: str = job["job_id"]
    chat_id: int = job["chat_id"]
    reply_to_message_id: int = job["reply_to_message_id"]
    status_message_id: int = job["status_message_id"]
    url: str = job["url"]
    mode: str = job["mode"]  # "video" or "doc" or "audio"
    title: str = job["title"]
    plan: Dict[str, Any] = job["plan"]

    os.makedirs(config.output_folder, exist_ok=True)

    tmp_id = str(round(time.time() * 1000))
    outtmpl = f"{config.output_folder}/{tmp_id}.%(ext)s"

    progress_state: Dict[str, Any] = {"pct": 0}

    def progress_hook(d: Dict[str, Any]):
        if _is_cancelled(job_id):
            raise RuntimeError("Cancelled by user")

        # Track only our real output file to avoid fake 100% flashes
        fn = d.get("filename") or d.get("tmpfilename") or ""
        if fn and tmp_id not in os.path.basename(fn):
            return

        if d.get("status") == "downloading":
            pct, done_b, total_b = _calc_download_progress(d, progress_state)

            # Safety: if yt-dlp reveals a hard total size > limit, abort immediately
            hard_total = d.get("total_bytes")
            if isinstance(hard_total, int) and hard_total > MAX_SEND_BYTES and mode != "audio":
                raise RuntimeError(
                    f"This video is too large: {_fmt_bytes(hard_total)} > limit {_fmt_bytes(MAX_SEND_BYTES)}"
                )

            _safe_edit(
                chat_id,
                status_message_id,
                _render_status(title, "downloading", pct, done_b, total_b),
                reply_markup=_cancel_markup(job_id)
            )

        elif d.get("status") == "finished":
            progress_state["pct"] = 100
            _safe_edit(
                chat_id,
                status_message_id,
                _render_status(title, "downloading", 100, None, None),
                reply_markup=_cancel_markup(job_id),
                force=True
            )

    ydl_opts: Dict[str, Any] = {
        "format": str(plan.get("format_spec", "best")),
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "max_filesize": MAX_SEND_BYTES,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": YTDLP_CONCURRENT_FRAGMENTS,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 20,
    }
    ydl_opts = _apply_youtube_runtime_opts(ydl_opts, url, YTDLP_JS_RUNTIMES, YTDLP_REMOTE_COMPONENTS)

    if plan.get("merge_output_format"):
        ydl_opts["merge_output_format"] = str(plan["merge_output_format"])

    if mode == "audio":
        mp3_kbps = int(plan.get("mp3_kbps", 128))
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(mp3_kbps),
        }]

    info: Dict[str, Any] = {}
    file_path: Optional[str] = None

    try:
        if _is_cancelled(job_id):
            _safe_delete(chat_id, status_message_id)
            return

        _safe_edit(
            chat_id,
            status_message_id,
            _render_status(title, "downloading", 0, None, None),
            reply_markup=_cancel_markup(job_id),
            force=True
        )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception:
            # Conservative one-shot fallback for YouTube extractor churn.
            if not _is_youtube_url(url) or mode == "audio":
                raise

            fallback_opts = dict(ydl_opts)
            fallback_opts["format"] = "18/best[ext=mp4]/best"
            fallback_opts["concurrent_fragment_downloads"] = 1
            with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                info = ydl.extract_info(url, download=True)

        if _is_cancelled(job_id):
            _safe_delete(chat_id, status_message_id)
            return

        prefer_ext = ".mp3" if mode == "audio" else None
        file_path = _find_downloaded_file(info, tmp_id, prefer_ext=prefer_ext)
        if not file_path:
            file_path = _find_file_by_prefix(tmp_id, prefer_ext=prefer_ext)

        if not file_path or not os.path.exists(file_path):
            raise RuntimeError("Downloaded file not found")

        # Final hard check before upload
        final_size = os.path.getsize(file_path)
        if final_size > MAX_SEND_BYTES:
            raise RuntimeError(
                f"This file is {_fmt_bytes(final_size)}, which exceeds the limit {_fmt_bytes(MAX_SEND_BYTES)}."
            )

        base = _sanitize_filename_base(title)

        if mode == "audio":
            send_filename = f"{base}.mp3"
            _send_via_bot_api_with_progress(
                job_id=job_id,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                status_message_id=status_message_id,
                title=title,
                method_name="sendAudio",
                file_field_name="audio",
                file_path=file_path,
                send_filename=send_filename,
                stage_label="Sending audio...",
                extra_params={},
            )
        elif mode == "doc":
            ext = os.path.splitext(file_path)[1] or ".mp4"
            send_filename = f"{base}{ext}"
            _send_via_bot_api_with_progress(
                job_id=job_id,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                status_message_id=status_message_id,
                title=title,
                method_name="sendDocument",
                file_field_name="document",
                file_path=file_path,
                send_filename=send_filename,
                stage_label="Sending document...",
                extra_params={},
            )
        else:
            ext = os.path.splitext(file_path)[1] or ".mp4"
            send_filename = f"{base}{ext}"
            _send_via_bot_api_with_progress(
                job_id=job_id,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
                status_message_id=status_message_id,
                title=title,
                method_name="sendVideo",
                file_field_name="video",
                file_path=file_path,
                send_filename=send_filename,
                stage_label="Sending video...",
                extra_params={"supports_streaming": "true"},
            )

        # Success: delete status message (only media remains)
        _safe_delete(chat_id, status_message_id)

    except Exception as e:
        if _is_cancelled(job_id):
            _safe_delete(chat_id, status_message_id)
        else:
            _safe_edit(chat_id, status_message_id, f"{title}\n\nStatus: ❌ {str(e)}", reply_markup=None, force=True)

    finally:
        # Cleanup local files
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

        try:
            for fn in os.listdir(config.output_folder):
                if fn.startswith(tmp_id):
                    fp = os.path.join(config.output_folder, fn)
                    if os.path.exists(fp):
                        os.remove(fp)
        except Exception:
            pass

        cancel_events.pop(job_id, None)
        active_jobs.pop(job_id, None)


def _worker_loop():
    while True:
        job = jobs_q.get()
        try:
            _download_and_send(job)
        finally:
            jobs_q.task_done()


for _ in range(WORKERS):
    t = threading.Thread(target=_worker_loop, daemon=True)
    t.start()


# =========================
# Logging (kept as-is)
# =========================
def log(message, text: str, media: str):
    if config.logs:
        if message.chat.type == "private":
            chat_info = "Private chat"
        else:
            chat_info = f"Group: *{message.chat.title}* (`{message.chat.id}`)"

        _bot_call(
            bot.send_message,
            config.logs,
            f"Download request ({media}) from @{message.from_user.username} ({message.from_user.id})\n\n{chat_info}\n\n{text}",
        )


# =========================
# Commands
# =========================
@bot.message_handler(commands=["start", "help"])
def start_help(message):
    bot.reply_to(
        message,
        "*Send me a video link* and I'll download it for you.\n\n"
        "You can choose:\n"
        "• *Video*\n"
        "• *Document* (original file)\n"
        "• *Audio (MP3)*\n\n"
        f"Upload limit: *{_fmt_bytes(MAX_SEND_BYTES)}*\n\n"
        "_Powered by_ [Avazbek Olimov](https://github.com/Avazbek22/VideoDownloaderBot)",
        parse_mode="MARKDOWN",
        disable_web_page_preview=True,
    )


# =========================
# Pending cleanup
# =========================
def _cleanup_pending() -> None:
    now = time.time()
    to_del = []
    for rid, data in pending_requests.items():
        if now - data.get("created_at", now) > PENDING_TTL_SEC:
            to_del.append(rid)
    for rid in to_del:
        pending_requests.pop(rid, None)


# =========================
# Main flow: message -> Getting info -> buttons
# (NO downloading unless size <= limit is proven)
# =========================
def _send_choice_ui(message, url: str) -> None:
    _cleanup_pending()

    processing_msg = bot.reply_to(message, "Getting info...", disable_web_page_preview=True)

    try:
        meta = _get_video_meta(url, js_runtimes=YTDLP_JS_RUNTIMES, remote_components=YTDLP_REMOTE_COMPONENTS)
    except Exception:
        _safe_delete(message.chat.id, processing_msg.message_id)
        bot.reply_to(message, "Invalid URL or unsupported website.", disable_web_page_preview=True)
        return

    title = (meta.get("title") or "Video").strip()
    title = _strip_hashtags(title) or "Video"

    # Build plans (no squeezing)
    video_plan = _build_video_plan_no_squeeze(meta)
    video_plan = _apply_probe_if_needed(video_plan)

    audio_plan, audio_reason = _build_audio_plan_mp3(meta, MAX_SEND_BYTES)

    _safe_delete(message.chat.id, processing_msg.message_id)

    # Decide availability for VIDEO/DOC:
    # We allow only if size is confident and <= limit.
    video_size = video_plan.get("estimated_size")
    video_conf = bool(video_plan.get("estimated_confident")) and isinstance(video_size, int) and video_size > 0
    video_ok = bool(video_conf and isinstance(video_size, int) and video_size <= MAX_SEND_BYTES)

    # If video is confidently too big -> tell and do not offer Video/Document.
    if video_conf and isinstance(video_size, int) and video_size > MAX_SEND_BYTES:
        msg = (
            f"{title}\n\n"
            f"This video is too large for Telegram bots.\n"
            f"Estimated size: {_fmt_bytes(video_size)}\n"
            f"Limit: {_fmt_bytes(MAX_SEND_BYTES)}\n"
        )

        # If audio fits, offer only audio button
        if audio_plan:
            request_id = uuid.uuid4().hex[:18]
            pending_requests[request_id] = {
                "created_at": time.time(),
                "user_id": message.from_user.id,
                "chat_id": message.chat.id,
                "reply_to_message_id": message.message_id,
                "url": url,
                "title": title,
                "video_plan": None,
                "audio_plan": audio_plan,
            }

            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Download as Audio (MP3)", callback_data=f"dl|audio|{request_id}"))

            _safe_send_message(
                chat_id=message.chat.id,
                text=msg + f"\nAudio option available: {audio_plan.get('quality_label', 'mp3')}",
                reply_to_message_id=message.message_id,
                reply_markup=kb
            )
            return

        # No audio either
        if audio_reason:
            msg += f"\nAudio is not available: {audio_reason}"
        _safe_send_message(message.chat.id, msg, reply_to_message_id=message.message_id)
        return

    # If we cannot confidently determine size -> do NOT download video/doc (policy to avoid wasting time/data)
    if not video_ok:
        msg = (
            f"{title}\n\n"
            f"I can't reliably determine the final video size before downloading.\n"
            f"Telegram bot upload limit is {_fmt_bytes(MAX_SEND_BYTES)}.\n"
            f"Please try a shorter video.\n"
        )

        # If audio fits, offer audio
        if audio_plan:
            request_id = uuid.uuid4().hex[:18]
            pending_requests[request_id] = {
                "created_at": time.time(),
                "user_id": message.from_user.id,
                "chat_id": message.chat.id,
                "reply_to_message_id": message.message_id,
                "url": url,
                "title": title,
                "video_plan": None,
                "audio_plan": audio_plan,
            }

            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Download as Audio (MP3)", callback_data=f"dl|audio|{request_id}"))

            _safe_send_message(
                chat_id=message.chat.id,
                text=msg + f"\nAudio option available: {audio_plan.get('quality_label', 'mp3')}",
                reply_to_message_id=message.message_id,
                reply_markup=kb
            )
            return

        if audio_reason:
            msg += f"\nAudio is not available: {audio_reason}"
        _safe_send_message(message.chat.id, msg, reply_to_message_id=message.message_id)
        return

    # If video_ok == True -> show normal 3 buttons (Video/Document/Audio if available)
    request_id = uuid.uuid4().hex[:18]
    pending_requests[request_id] = {
        "created_at": time.time(),
        "user_id": message.from_user.id,
        "chat_id": message.chat.id,
        "reply_to_message_id": message.message_id,
        "url": url,
        "title": title,
        "video_plan": video_plan,
        "audio_plan": audio_plan,
    }

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Download as Video", callback_data=f"dl|video|{request_id}"),
        types.InlineKeyboardButton("Download as Document", callback_data=f"dl|doc|{request_id}"),
    )
    if audio_plan:
        kb.add(types.InlineKeyboardButton("Download as Audio (MP3)", callback_data=f"dl|audio|{request_id}"))

    info_lines = [
        f"Estimated size: {_fmt_bytes(int(video_size))} (limit {_fmt_bytes(MAX_SEND_BYTES)})",
        f"Selected: {video_plan.get('quality_label', 'mp4')}",
    ]
    if audio_plan:
        info_lines.append(f"Audio: {audio_plan.get('quality_label', 'mp3')}")

    _safe_send_message(
        chat_id=message.chat.id,
        text=f"{title}\n\nChoose download method:\n" + "\n".join(info_lines),
        reply_to_message_id=message.message_id,
        reply_markup=kb
    )


@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "video", "document", "audio", "voice"])
def handle_private_messages(message):
    if message.chat.type != "private":
        return

    text = message.text if message.text else message.caption if message.caption else None
    if not text:
        return

    if isinstance(text, str) and text.strip().startswith("/"):
        return

    url = _extract_first_url(text)
    if not url:
        return

    url_info = urlparse(url)
    if not url_info.scheme:
        bot.reply_to(message, "Invalid URL", disable_web_page_preview=True)
        return

    if url_info.netloc in ["www.youtube.com", "youtu.be", "youtube.com", "youtu.be"]:
        if not youtube_url_validation(url):
            bot.reply_to(message, "Invalid URL", disable_web_page_preview=True)
            return

    log(message, url, "video")
    _send_choice_ui(message, url)


# =========================
# Callback: cancel
# =========================
@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("cnl|")))
def on_cancel(call):
    try:
        parts = call.data.split("|")
        if len(parts) != 2:
            _safe_answer_callback(call.id, "Invalid action")
            return

        job_id = parts[1]
        job_info = active_jobs.get(job_id)
        if not job_info:
            _safe_answer_callback(call.id, "Nothing to cancel.")
            return

        if call.from_user.id != job_info.get("user_id"):
            _safe_answer_callback(call.id, "This is not your request.")
            return

        ev = cancel_events.get(job_id)
        if ev:
            ev.set()

        chat_id = job_info.get("chat_id")
        status_mid = job_info.get("status_message_id")
        if isinstance(chat_id, int) and isinstance(status_mid, int):
            _safe_delete(chat_id, status_mid)

        _safe_answer_callback(call.id, "Cancelled.")
    except Exception:
        _safe_answer_callback(call.id, "Error")


# =========================
# Callback: buttons -> enqueue job
# =========================
@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("dl|")))
def on_download_choice(call):
    try:
        parts = call.data.split("|")
        if len(parts) != 3:
            _safe_answer_callback(call.id, "Invalid action")
            return

        mode = parts[1]  # video/doc/audio
        rid = parts[2]

        req = pending_requests.get(rid)
        if not req:
            _safe_answer_callback(call.id, "Request expired. Send the link again.")
            return

        if call.from_user.id != req["user_id"]:
            _safe_answer_callback(call.id, "This is not your request.")
            return

        pending_requests.pop(rid, None)

        chat_id = req["chat_id"]
        reply_to_message_id = req["reply_to_message_id"]
        status_message_id = call.message.message_id
        url = req["url"]
        title = req["title"]

        if mode == "audio":
            plan = req.get("audio_plan")
            if not plan:
                _safe_answer_callback(call.id, "Audio is not available.")
                return
            job_mode = "audio"
        elif mode == "doc":
            plan = req.get("video_plan")
            if not plan:
                _safe_answer_callback(call.id, "Video is not available.")
                return
            job_mode = "doc"
        else:
            plan = req.get("video_plan")
            if not plan:
                _safe_answer_callback(call.id, "Video is not available.")
                return
            job_mode = "video"

        _safe_answer_callback(call.id, "OK")

        job_id = uuid.uuid4().hex[:18]
        cancel_events[job_id] = threading.Event()
        active_jobs[job_id] = {
            "user_id": req["user_id"],
            "chat_id": chat_id,
            "status_message_id": status_message_id,
        }

        queued_pos = jobs_q.qsize() + 1
        _safe_edit(
            chat_id,
            status_message_id,
            _render_status(title, "queued", None, None, None, queued_pos=queued_pos),
            reply_markup=_cancel_markup(job_id),
            force=True
        )

        job = {
            "job_id": job_id,
            "chat_id": chat_id,
            "reply_to_message_id": reply_to_message_id,
            "status_message_id": status_message_id,
            "url": url,
            "title": title,
            "mode": job_mode,
            "plan": plan,
        }
        jobs_q.put(job)

    except Exception:
        _safe_answer_callback(call.id, "Error")


# =========================
# Keep your /custom as-is
# =========================
def get_text(message):
    if not message.text:
        return None
    if len(message.text.split(" ")) < 2:
        if message.reply_to_message and message.reply_to_message.text:
            return message.reply_to_message.text
        return None
    return message.text.split(" ")[1]


@bot.message_handler(commands=["custom"])
def custom(message):
    text = get_text(message)
    if not text:
        bot.reply_to(message, "Invalid usage, use `/custom url`", parse_mode="MARKDOWN")
        return

    msg = bot.reply_to(message, "Getting formats...", disable_web_page_preview=True)

    try:
        info = _get_video_meta(text, js_runtimes=YTDLP_JS_RUNTIMES, remote_components=YTDLP_REMOTE_COMPONENTS)

        data = {
            f"{x.get('resolution')}.{x.get('ext')}": {"callback_data": f"{x.get('format_id')}"}
            for x in info.get("formats", [])
            if x.get("video_ext") != "none"
        }

        markup = quick_markup(data, row_width=2)

        _safe_delete(msg.chat.id, msg.message_id)
        bot.reply_to(message, "Choose a format", reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        _safe_delete(msg.chat.id, msg.message_id)
        bot.reply_to(message, "Failed to get formats.", disable_web_page_preview=True)


@bot.callback_query_handler(func=lambda call: bool(call.data) and not call.data.startswith("dl|") and not call.data.startswith("cnl|"))
def callback_custom_format(call):
    try:
        if not call.message.reply_to_message:
            return
        if call.from_user.id != call.message.reply_to_message.from_user.id:
            _safe_answer_callback(call.id, "You didn't send the request")
            return

        url = get_text(call.message.reply_to_message)
        if not url:
            _safe_answer_callback(call.id, "No URL")
            return

        _safe_delete(call.message.chat.id, call.message.message_id)

        _send_choice_ui(call.message.reply_to_message, url)

        _safe_answer_callback(call.id, "OK")
    except Exception:
        pass


# =========================
# Run
# =========================
bot.infinity_polling()
