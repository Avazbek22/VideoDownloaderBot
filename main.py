import datetime
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

import telebot
import yt_dlp
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from telebot import types
from telebot.util import quick_markup

from app.download_utils import (
    calc_download_progress as _calc_download_progress,
)
from app.download_utils import (
    find_downloaded_file as _find_downloaded_file_impl,
)
from app.download_utils import (
    find_file_by_prefix as _find_file_by_prefix_impl,
)
from app.download_utils import (
    render_status as _render_status,
)
from app.http_utils import telegram_upload_session
from app.logging_setup import configure_logging
from app.models import ActiveJob, DownloadJob, PendingRequest
from app.planner import (
    apply_instagram_stability_opts as _apply_instagram_stability_opts,
)
from app.planner import (
    apply_probe_if_needed as _apply_probe_if_needed,
)
from app.planner import (
    apply_youtube_runtime_opts as _apply_youtube_runtime_opts,
)
from app.planner import (
    build_audio_plan_mp3 as _build_audio_plan_mp3,
)
from app.planner import (
    build_video_plan_no_squeeze as _build_video_plan_no_squeeze,
)
from app.planner import (
    get_video_meta as _get_video_meta,
)
from app.planner import (
    is_instagram_url as _is_instagram_url,
)
from app.planner import (
    is_youtube_url as _is_youtube_url,
)
from app.settings import Settings, load_settings
from app.temp_files import cleanup_job_directory, cleanup_stale_directories
from app.text_utils import (
    extract_first_url as _extract_first_url,
)
from app.text_utils import (
    fmt_bytes as _fmt_bytes,
)
from app.text_utils import (
    sanitize_filename_base as _sanitize_filename_base,
)
from app.text_utils import (
    strip_hashtags as _strip_hashtags,
)
from app.text_utils import (
    youtube_url_validation,
)
from app.url_security import UnsafeUrlError, safe_url_for_log, validate_public_url

# =========================
# Telegram bot init
# =========================
bot: telebot.TeleBot | None = None
SETTINGS: Settings | None = None
LOGGER = logging.getLogger("video_downloader_bot")

# Edit throttling (avoid Telegram flood limits)
EDIT_INTERVAL_SEC = 1.8

# Parallel jobs: how many downloads/uploads can run simultaneously
WORKERS = 2

# TTL for pending "choice" requests to avoid memory leaks
PENDING_TTL_SEC = 10 * 60

# Telegram Bot API upload limit (you keep it in config)
MAX_SEND_BYTES = 50 * 1024 * 1024

# yt-dlp optimization for segmented streams (HLS/DASH)
YTDLP_CONCURRENT_FRAGMENTS = 4
# YouTube JS challenge runtime settings (stable defaults, no account/cookies required)
YTDLP_JS_RUNTIMES = (os.getenv("YTDLP_JS_RUNTIMES") or "node").strip() or "node"
YTDLP_REMOTE_COMPONENTS = (os.getenv("YTDLP_REMOTE_COMPONENTS") or "ejs:github").strip() or "ejs:github"
# Instagram stability profile (no account/cookies required)
YTDLP_INSTAGRAM_IMPERSONATE = (os.getenv("YTDLP_INSTAGRAM_IMPERSONATE") or "chrome").strip() or "chrome"
YTDLP_INSTAGRAM_RETRIES = int(os.getenv("YTDLP_INSTAGRAM_RETRIES") or "8")
YTDLP_INSTAGRAM_FRAGMENT_RETRIES = int(os.getenv("YTDLP_INSTAGRAM_FRAGMENT_RETRIES") or "8")
YTDLP_INSTAGRAM_SOCKET_TIMEOUT = int(os.getenv("YTDLP_INSTAGRAM_SOCKET_TIMEOUT") or "30")


# =========================
# Global state
# =========================
bot_lock = threading.RLock()
state_lock = threading.RLock()
stop_event = threading.Event()

last_edited: dict[str, datetime.datetime] = {}
last_text: dict[str, str] = {}

pending_requests: dict[str, PendingRequest] = {}
jobs_q: "queue.Queue[DownloadJob | None]" = queue.Queue(maxsize=200)

# Cancel support
cancel_events: dict[str, threading.Event] = {}
active_jobs: dict[str, ActiveJob] = {}
worker_threads: list[threading.Thread] = []
maintenance_thread: threading.Thread | None = None
upload_slots = threading.BoundedSemaphore(2)


class JobCancelled(RuntimeError):
    pass


class JobTimedOut(RuntimeError):
    pass


def _settings() -> Settings:
    if SETTINGS is None:
        raise RuntimeError("application is not initialized")
    return SETTINGS


def _output_folder() -> Path:
    return _settings().output_dir


def _check_job(job_id: str, deadline: float) -> None:
    if _is_cancelled(job_id):
        raise JobCancelled("cancelled")
    if time.monotonic() >= deadline:
        raise JobTimedOut("job deadline exceeded")


# =========================
# Helpers (safe bot calls)
# =========================
def _bot_call(fn, *args, **kwargs):
    with bot_lock:
        return fn(*args, **kwargs)


def _safe_delete(chat_id: int, message_id: int) -> None:
    try:
        assert bot is not None
        _bot_call(bot.delete_message, chat_id, message_id)
    except Exception:
        LOGGER.debug("telegram delete failed chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)


def _safe_edit(chat_id: int, message_id: int, text: str, reply_markup=None, force: bool = False) -> None:
    key = f"{chat_id}-{message_id}"
    now = datetime.datetime.now()

    if not force:
        with state_lock:
            last = last_edited.get(key)
            if last is not None and (now - last).total_seconds() < EDIT_INTERVAL_SEC:
                return
            if last_text.get(key) == text:
                return

    try:
        assert bot is not None
        _bot_call(
            bot.edit_message_text,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        with state_lock:
            last_edited[key] = now
            last_text[key] = text
    except Exception:
        LOGGER.debug("telegram edit failed chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)


def _safe_send_message(chat_id: int, text: str, reply_to_message_id: int | None = None, reply_markup=None):
    try:
        assert bot is not None
        return _bot_call(
            bot.send_message,
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        LOGGER.warning("telegram send message failed chat_id=%s", chat_id, exc_info=True)
        return None


def _safe_answer_callback(call_id: str, text: str = "") -> None:
    try:
        assert bot is not None
        _bot_call(bot.answer_callback_query, call_id, text=text)
    except Exception:
        LOGGER.debug("telegram callback answer failed", exc_info=True)


def _notify_operator_critical(text: str) -> None:
    if SETTINGS is None or SETTINGS.logs_chat_id is None or bot is None:
        return
    try:
        _bot_call(bot.send_message, SETTINGS.logs_chat_id, text, disable_web_page_preview=True)
    except Exception:
        LOGGER.exception("operator critical notification failed")


# =========================
# Cancel UI
# =========================
def _cancel_markup(job_id: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("Cancel", callback_data=f"cnl|{job_id}"))
    return kb


def _is_cancelled(job_id: str) -> bool:
    with state_lock:
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
    extra_params: dict[str, Any],
    deadline: float,
) -> None:
    api_url = f"https://api.telegram.org/bot{_settings().token}/{method_name}"
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    def render_upload(pct: int | None, sent: int | None, total_len: int | None) -> str:
        line = f"Status: ⬆️ {stage_label}"
        if pct is not None:
            pct = max(0, min(100, int(pct)))
            line += f" {pct}%"
        if isinstance(sent, int) and isinstance(total_len, int) and total_len > 0:
            line += f"\n{_fmt_bytes(sent)} / {_fmt_bytes(total_len)}"
        return f"{title}\n\n{line}"

    _safe_edit(
        chat_id, status_message_id, render_upload(0, 0, file_size), reply_markup=_cancel_markup(job_id), force=True
    )

    _check_job(job_id, deadline)

    remaining = max(0.0, deadline - time.monotonic())
    if not upload_slots.acquire(timeout=remaining):
        raise JobTimedOut("upload slot deadline exceeded")

    try:
        with open(file_path, "rb") as f:
            fields = {
                "chat_id": str(chat_id),
                "reply_to_message_id": str(reply_to_message_id),
                **{k: str(v) for k, v in extra_params.items() if v is not None},
                file_field_name: (send_filename, f),
            }

            encoder = MultipartEncoder(fields=fields)

            def _cb(monitor: MultipartEncoderMonitor):
                _check_job(job_id, deadline)

                total_len = monitor.len
                sent = monitor.bytes_read
                pct = int((sent * 100) / total_len) if total_len else None
                _safe_edit(
                    chat_id,
                    status_message_id,
                    render_upload(pct, sent, total_len),
                    reply_markup=_cancel_markup(job_id),
                )

            monitor = MultipartEncoderMonitor(encoder, _cb)

            session = telegram_upload_session()
            try:
                remaining = max(1.0, deadline - time.monotonic())
                resp = session.post(
                    api_url,
                    data=monitor,
                    headers={"Content-Type": monitor.content_type},
                    timeout=(20, min(60 * 30, remaining)),
                )
            finally:
                session.close()
    finally:
        upload_slots.release()

    _parse_upload_response(resp)

    _safe_edit(
        chat_id,
        status_message_id,
        render_upload(100, file_size, file_size),
        reply_markup=_cancel_markup(job_id),
        force=True,
    )


def _parse_upload_response(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"Telegram API returned HTTP {response.status_code}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description", "request failed") if isinstance(data, dict) else "request failed"
        raise RuntimeError(f"Telegram API error: {description}")
    return data


# =========================
# Downloaded file discovery
# =========================
def _find_file_by_prefix(output_folder: str, prefix: str, prefer_ext: str | None = None) -> str | None:
    return _find_file_by_prefix_impl(output_folder, prefix, prefer_ext=prefer_ext)


def _find_downloaded_file(
    info: dict[str, Any], output_folder: str, fallback_prefix: str, prefer_ext: str | None = None
) -> str | None:
    return _find_downloaded_file_impl(info, output_folder, fallback_prefix, prefer_ext=prefer_ext)


def _get_video_meta_with_hidden_retries(url: str) -> dict[str, Any]:
    try:
        meta = _get_video_meta(
            url,
            js_runtimes=YTDLP_JS_RUNTIMES,
            remote_components=YTDLP_REMOTE_COMPONENTS,
            instagram_impersonate=YTDLP_INSTAGRAM_IMPERSONATE,
            instagram_retries=YTDLP_INSTAGRAM_RETRIES,
            instagram_fragment_retries=YTDLP_INSTAGRAM_FRAGMENT_RETRIES,
            instagram_socket_timeout=YTDLP_INSTAGRAM_SOCKET_TIMEOUT,
            cookies_file=str(_settings().cookies_file) if _settings().cookies_file else None,
        )
        _validate_metadata_urls(meta)
        return meta
    except Exception:
        if not _is_instagram_url(url):
            raise
        LOGGER.debug("Instagram metadata primary attempt failed; using fallback", exc_info=True)
        # Fallback: retry without forced impersonation.
        meta = _get_video_meta(
            url,
            js_runtimes=YTDLP_JS_RUNTIMES,
            remote_components=YTDLP_REMOTE_COMPONENTS,
            instagram_impersonate=None,
            instagram_retries=5,
            instagram_fragment_retries=5,
            instagram_socket_timeout=20,
            cookies_file=str(_settings().cookies_file) if _settings().cookies_file else None,
        )
        _validate_metadata_urls(meta)
        return meta


def _validate_metadata_urls(meta: dict[str, Any]) -> None:
    candidates = [meta.get(key) for key in ("webpage_url", "original_url", "url")]
    candidates.extend(item.get("url") for item in meta.get("formats", []) if isinstance(item, dict))
    checked_hosts: set[tuple[str, int | None]] = set()
    for value in candidates:
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            continue
        parts = urlsplit(value)
        host_key = ((parts.hostname or "").lower(), parts.port)
        if host_key in checked_hosts:
            continue
        validate_public_url(value)
        checked_hosts.add(host_key)


# =========================
# Worker: download + send
# =========================
def _download_and_send(job: DownloadJob) -> None:
    job_id = job.job_id
    chat_id = job.chat_id
    reply_to_message_id = job.reply_to_message_id
    status_message_id = job.status_message_id
    url = job.url
    mode = job.mode
    title = job.title
    plan = job.plan
    deadline = job.deadline

    job_dir = _output_folder() / job_id
    try:
        job_dir.mkdir(parents=True, exist_ok=False)
    except Exception:
        LOGGER.exception("job workspace creation failed job_id=%s", job_id)
        _safe_edit(
            chat_id,
            status_message_id,
            f"{title}\n\nStatus: ❌ Download failed. Please try again.",
            reply_markup=None,
            force=True,
        )
        with state_lock:
            cancel_events.pop(job_id, None)
            active_jobs.pop(job_id, None)
        return
    tmp_id = uuid.uuid4().hex
    outtmpl = os.fspath(job_dir / f"{tmp_id}.%(ext)s")

    progress_state: dict[str, Any] = {"pct": 0}

    def progress_hook(d: dict[str, Any]):
        _check_job(job_id, deadline)

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
                reply_markup=_cancel_markup(job_id),
            )

        elif d.get("status") == "finished":
            progress_state["pct"] = 100
            _safe_edit(
                chat_id,
                status_message_id,
                _render_status(title, "downloading", 100, None, None),
                reply_markup=_cancel_markup(job_id),
                force=True,
            )

    ydl_opts: dict[str, Any] = {
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
        "postprocessor_hooks": [lambda _status: _check_job(job_id, deadline)],
    }
    if _settings().cookies_file:
        ydl_opts["cookiefile"] = os.fspath(_settings().cookies_file)
    ydl_opts = _apply_youtube_runtime_opts(ydl_opts, url, YTDLP_JS_RUNTIMES, YTDLP_REMOTE_COMPONENTS)
    ydl_opts = _apply_instagram_stability_opts(
        ydl_opts,
        url,
        impersonate=YTDLP_INSTAGRAM_IMPERSONATE,
        retries=YTDLP_INSTAGRAM_RETRIES,
        fragment_retries=YTDLP_INSTAGRAM_FRAGMENT_RETRIES,
        socket_timeout=YTDLP_INSTAGRAM_SOCKET_TIMEOUT,
    )

    if plan.get("merge_output_format"):
        ydl_opts["merge_output_format"] = str(plan["merge_output_format"])

    if mode == "audio":
        mp3_kbps = int(plan.get("mp3_kbps", 128))
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(mp3_kbps),
            }
        ]

    info: dict[str, Any] = {}
    file_path: str | None = None

    try:
        _check_job(job_id, deadline)

        _safe_edit(
            chat_id,
            status_message_id,
            _render_status(title, "downloading", 0, None, None),
            reply_markup=_cancel_markup(job_id),
            force=True,
        )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception:
            _check_job(job_id, deadline)
            LOGGER.debug("primary download attempt failed job_id=%s; evaluating fallback", job_id, exc_info=True)
            # Conservative one-shot fallback for YouTube extractor churn.
            if _is_youtube_url(url) and mode != "audio":
                fallback_opts = dict(ydl_opts)
                fallback_opts["format"] = "18/best[ext=mp4]/best"
                fallback_opts["concurrent_fragment_downloads"] = 1
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            elif _is_instagram_url(url):
                fallback_opts = dict(ydl_opts)
                fallback_opts.pop("impersonate", None)
                fallback_opts["retries"] = 5
                fallback_opts["fragment_retries"] = 5
                fallback_opts["socket_timeout"] = 20
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            else:
                raise

        _check_job(job_id, deadline)

        prefer_ext = ".mp3" if mode == "audio" else None
        file_path = _find_downloaded_file(info, os.fspath(job_dir), tmp_id, prefer_ext=prefer_ext)
        if not file_path:
            file_path = _find_file_by_prefix(os.fspath(job_dir), tmp_id, prefer_ext=prefer_ext)

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
                deadline=deadline,
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
                deadline=deadline,
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
                deadline=deadline,
            )

        # Success: delete status message (only media remains)
        _safe_delete(chat_id, status_message_id)

    except JobCancelled:
        LOGGER.info("job cancelled job_id=%s stage=processing", job_id)
        _safe_delete(chat_id, status_message_id)
    except JobTimedOut:
        LOGGER.warning("job timed out job_id=%s stage=processing", job_id)
        _safe_edit(
            chat_id,
            status_message_id,
            f"{title}\n\nStatus: ❌ Job timed out. Please try again.",
            reply_markup=None,
            force=True,
        )
    except Exception:
        LOGGER.exception("job failed job_id=%s stage=processing url=%s", job_id, safe_url_for_log(url))
        _notify_operator_critical(f"Critical job failure\njob_id={job_id}\nstage=processing")
        if _is_cancelled(job_id):
            _safe_delete(chat_id, status_message_id)
        else:
            _safe_edit(
                chat_id,
                status_message_id,
                f"{title}\n\nStatus: ❌ Download failed. Please try again.",
                reply_markup=None,
                force=True,
            )

    finally:
        cleanup_job_directory(job_dir)
        with state_lock:
            cancel_events.pop(job_id, None)
            active_jobs.pop(job_id, None)


def _worker_loop():
    while not stop_event.is_set():
        job = jobs_q.get()
        try:
            if job is None:
                return
            _download_and_send(job)
        except Exception:
            LOGGER.exception("worker crashed while processing a job")
        finally:
            jobs_q.task_done()


# =========================
# Logging (kept as-is)
# =========================
def log(message, text: str, media: str):
    LOGGER.info(
        "download request media=%s user_id=%s chat_id=%s url=%s",
        media,
        message.from_user.id,
        message.chat.id,
        safe_url_for_log(text),
    )


# =========================
# Commands
# =========================
def start_help(message):
    assert bot is not None
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
    with state_lock:
        expired = [rid for rid, data in pending_requests.items() if now - data.created_at > PENDING_TTL_SEC]
        for rid in expired:
            pending_requests.pop(rid, None)
        stale_edit_keys = [
            key
            for key, edited_at in last_edited.items()
            if (datetime.datetime.now() - edited_at).total_seconds() > PENDING_TTL_SEC
        ]
        for key in stale_edit_keys:
            last_edited.pop(key, None)
            last_text.pop(key, None)


# =========================
# Main flow: message -> Getting info -> buttons
# (NO downloading unless size <= limit is proven)
# =========================
def _send_choice_ui(message, url: str) -> None:
    assert bot is not None
    _cleanup_pending()

    processing_msg = bot.reply_to(message, "Getting info...", disable_web_page_preview=True)

    try:
        meta = _get_video_meta_with_hidden_retries(url)
    except Exception:
        LOGGER.exception("metadata failed url=%s", safe_url_for_log(url))
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
            with state_lock:
                pending_requests[request_id] = PendingRequest(
                    created_at=time.time(),
                    user_id=message.from_user.id,
                    chat_id=message.chat.id,
                    reply_to_message_id=message.message_id,
                    url=url,
                    title=title,
                    video_plan=None,
                    audio_plan=audio_plan,
                )

            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Download as Audio (MP3)", callback_data=f"dl|audio|{request_id}"))

            _safe_send_message(
                chat_id=message.chat.id,
                text=msg + f"\nAudio option available: {audio_plan.get('quality_label', 'mp3')}",
                reply_to_message_id=message.message_id,
                reply_markup=kb,
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
            with state_lock:
                pending_requests[request_id] = PendingRequest(
                    created_at=time.time(),
                    user_id=message.from_user.id,
                    chat_id=message.chat.id,
                    reply_to_message_id=message.message_id,
                    url=url,
                    title=title,
                    video_plan=None,
                    audio_plan=audio_plan,
                )

            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("Download as Audio (MP3)", callback_data=f"dl|audio|{request_id}"))

            _safe_send_message(
                chat_id=message.chat.id,
                text=msg + f"\nAudio option available: {audio_plan.get('quality_label', 'mp3')}",
                reply_to_message_id=message.message_id,
                reply_markup=kb,
            )
            return

        if audio_reason:
            msg += f"\nAudio is not available: {audio_reason}"
        _safe_send_message(message.chat.id, msg, reply_to_message_id=message.message_id)
        return

    # If video_ok == True -> show normal 3 buttons (Video/Document/Audio if available)
    request_id = uuid.uuid4().hex[:18]
    with state_lock:
        pending_requests[request_id] = PendingRequest(
            created_at=time.time(),
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            reply_to_message_id=message.message_id,
            url=url,
            title=title,
            video_plan=video_plan,
            audio_plan=audio_plan,
        )

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
        reply_markup=kb,
    )


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

    try:
        validate_public_url(url)
    except UnsafeUrlError:
        bot.reply_to(message, "Invalid URL", disable_web_page_preview=True)
        return

    url_info = urlparse(url)

    if url_info.netloc in ["www.youtube.com", "youtu.be", "youtube.com", "youtu.be"] and not youtube_url_validation(
        url
    ):
        bot.reply_to(message, "Invalid URL", disable_web_page_preview=True)
        return

    log(message, url, "video")
    _send_choice_ui(message, url)


# =========================
# Callback: cancel
# =========================
def on_cancel(call):
    try:
        parts = call.data.split("|")
        if len(parts) != 2:
            _safe_answer_callback(call.id, "Invalid action")
            return

        job_id = parts[1]
        with state_lock:
            job_info = active_jobs.get(job_id)
        if not job_info:
            _safe_answer_callback(call.id, "Nothing to cancel.")
            return

        if call.from_user.id != job_info.user_id:
            _safe_answer_callback(call.id, "This is not your request.")
            return

        ev = job_info.cancel_event
        if ev:
            ev.set()

        chat_id = job_info.chat_id
        status_mid = job_info.status_message_id
        if isinstance(chat_id, int) and isinstance(status_mid, int):
            _safe_delete(chat_id, status_mid)

        _safe_answer_callback(call.id, "Cancelled.")
    except Exception:
        LOGGER.exception("cancel callback failed")
        _safe_answer_callback(call.id, "Error")


# =========================
# Callback: buttons -> enqueue job
# =========================
def on_download_choice(call):
    try:
        parts = call.data.split("|")
        if len(parts) != 3:
            _safe_answer_callback(call.id, "Invalid action")
            return

        mode = parts[1]  # video/doc/audio
        rid = parts[2]

        with state_lock:
            req = pending_requests.get(rid)
            if req is not None and time.time() - req.created_at > PENDING_TTL_SEC:
                pending_requests.pop(rid, None)
                req = None
            if req is not None and call.from_user.id == req.user_id:
                pending_requests.pop(rid, None)

        if not req:
            _safe_answer_callback(call.id, "Request expired. Send the link again.")
            return

        if call.from_user.id != req.user_id:
            _safe_answer_callback(call.id, "This is not your request.")
            return

        chat_id = req.chat_id
        reply_to_message_id = req.reply_to_message_id
        status_message_id = call.message.message_id
        url = req.url
        title = req.title

        if mode == "audio":
            plan = req.audio_plan
            if not plan:
                _safe_answer_callback(call.id, "Audio is not available.")
                return
            job_mode = "audio"
        elif mode == "doc":
            plan = req.video_plan
            if not plan:
                _safe_answer_callback(call.id, "Video is not available.")
                return
            job_mode = "doc"
        else:
            plan = req.video_plan
            if not plan:
                _safe_answer_callback(call.id, "Video is not available.")
                return
            job_mode = "video"

        _safe_answer_callback(call.id, "OK")

        job_id = uuid.uuid4().hex[:18]
        cancel_event = threading.Event()
        with state_lock:
            cancel_events[job_id] = cancel_event
            active_jobs[job_id] = ActiveJob(
                user_id=req.user_id,
                chat_id=chat_id,
                status_message_id=status_message_id,
                cancel_event=cancel_event,
            )

        queued_pos = jobs_q.qsize() + 1
        _safe_edit(
            chat_id,
            status_message_id,
            _render_status(title, "queued", None, None, None, queued_pos=queued_pos),
            reply_markup=_cancel_markup(job_id),
            force=True,
        )

        job = DownloadJob(
            job_id=job_id,
            user_id=req.user_id,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            status_message_id=status_message_id,
            url=url,
            title=title,
            mode=job_mode,
            plan=plan,
            deadline=time.monotonic() + _settings().job_timeout_seconds,
        )
        try:
            jobs_q.put_nowait(job)
        except queue.Full:
            with state_lock:
                cancel_events.pop(job_id, None)
                active_jobs.pop(job_id, None)
            _safe_edit(
                chat_id,
                status_message_id,
                f"{title}\n\nStatus: ❌ Queue is full. Please try again later.",
                reply_markup=None,
                force=True,
            )

    except Exception:
        LOGGER.exception("download callback failed")
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


def custom(message):
    assert bot is not None
    text = get_text(message)
    if not text:
        bot.reply_to(message, "Invalid usage, use `/custom url`", parse_mode="MARKDOWN")
        return

    msg = bot.reply_to(message, "Getting formats...", disable_web_page_preview=True)

    try:
        info = _get_video_meta_with_hidden_retries(text)

        data = {
            f"{x.get('resolution')}.{x.get('ext')}": {"callback_data": f"{x.get('format_id')}"}
            for x in info.get("formats", [])
            if x.get("video_ext") != "none"
        }

        markup = quick_markup(data, row_width=2)

        _safe_delete(msg.chat.id, msg.message_id)
        bot.reply_to(message, "Choose a format", reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        LOGGER.exception("custom formats metadata failed url=%s", safe_url_for_log(text))
        _safe_delete(msg.chat.id, msg.message_id)
        bot.reply_to(message, "Failed to get formats.", disable_web_page_preview=True)


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
        LOGGER.exception("custom callback failed")


def register_handlers(instance: telebot.TeleBot) -> None:
    instance.message_handler(commands=["start", "help"])(start_help)
    instance.message_handler(commands=["custom"])(custom)
    instance.message_handler(
        func=lambda m: True,
        content_types=["text", "photo", "video", "document", "audio", "voice"],
    )(handle_private_messages)
    instance.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("cnl|")))(on_cancel)
    instance.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("dl|")))(
        on_download_choice
    )
    instance.callback_query_handler(
        func=lambda call: bool(call.data) and not call.data.startswith("dl|") and not call.data.startswith("cnl|")
    )(callback_custom_format)


def _preflight(settings: Settings, instance: telebot.TeleBot) -> None:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    probe = settings.output_dir / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    if settings.cookies_file and not settings.cookies_file.is_file():
        raise RuntimeError(f"COOKIES_FILE does not exist: {settings.cookies_file}")
    if settings.cookies_file and not os.access(settings.cookies_file, os.R_OK):
        raise RuntimeError(f"COOKIES_FILE is not readable: {settings.cookies_file}")
    for executable, version_flag in (("ffmpeg", "-version"), ("node", "--version")):
        path = shutil.which(executable)
        if not path:
            raise RuntimeError(f"required executable is unavailable: {executable}")
        subprocess.run(
            [path, version_flag],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        LOGGER.info("preflight executable=%s path=%s", executable, path)
    LOGGER.info("preflight yt-dlp version=%s", yt_dlp.version.__version__)
    identity = instance.get_me()
    LOGGER.info("preflight Telegram getMe ok bot_id=%s username=%s", identity.id, identity.username)


def _maintenance_loop() -> None:
    while not stop_event.wait(300):
        try:
            _cleanup_pending()
            cleanup_stale_directories(_output_folder(), _settings().job_timeout_seconds * 2)
            health_file = _output_folder().parent / ".healthy"
            health_file.touch(exist_ok=True)
        except Exception:
            LOGGER.exception("periodic maintenance failed")


def startup(settings: Settings) -> telebot.TeleBot:
    global SETTINGS, bot, WORKERS, PENDING_TTL_SEC, MAX_SEND_BYTES
    global YTDLP_CONCURRENT_FRAGMENTS, jobs_q, upload_slots, maintenance_thread

    SETTINGS = settings
    WORKERS = settings.workers
    PENDING_TTL_SEC = settings.pending_ttl_seconds
    MAX_SEND_BYTES = settings.max_filesize
    YTDLP_CONCURRENT_FRAGMENTS = settings.concurrent_fragments
    jobs_q = queue.Queue(maxsize=settings.max_queue)
    upload_slots = threading.BoundedSemaphore(settings.upload_workers)
    stop_event.clear()

    bot = telebot.TeleBot(settings.token, threaded=True)
    register_handlers(bot)
    _preflight(settings, bot)
    cleanup_stale_directories(settings.output_dir, settings.job_timeout_seconds * 2)
    (settings.output_dir.parent / ".healthy").touch(exist_ok=True)

    worker_threads.clear()
    for index in range(settings.workers):
        thread = threading.Thread(target=_worker_loop, name=f"download-worker-{index + 1}", daemon=False)
        thread.start()
        worker_threads.append(thread)
    maintenance_thread = threading.Thread(target=_maintenance_loop, name="maintenance", daemon=False)
    maintenance_thread.start()
    LOGGER.info("application started workers=%s max_queue=%s", settings.workers, settings.max_queue)
    return bot


def stop() -> None:
    stop_event.set()
    with state_lock:
        events = list(cancel_events.values())
    for event in events:
        event.set()
    if bot is not None:
        try:
            bot.stop_bot()
        except Exception:
            LOGGER.exception("failed to stop Telegram bot worker pool")
    for _ in worker_threads:
        try:
            jobs_q.put(None, timeout=5)
        except queue.Full:
            LOGGER.error("could not enqueue worker shutdown sentinel")
    for thread in worker_threads:
        thread.join(timeout=25)
        if thread.is_alive():
            LOGGER.error("worker did not stop gracefully name=%s", thread.name)
    if maintenance_thread is not None:
        maintenance_thread.join(timeout=5)
    LOGGER.info("application stopped")


def run() -> None:
    if bot is None:
        raise RuntimeError("application is not started")
    bot.infinity_polling()


def main() -> None:
    settings = load_settings()
    configure_logging(settings.logs_dir, settings.log_level)
    instance: telebot.TeleBot | None = None
    try:
        instance = startup(settings)

        def _signal_handler(signum, _frame) -> None:
            LOGGER.info("received signal=%s", signum)
            if instance is not None:
                instance.stop_polling()

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        run()
    except Exception:
        LOGGER.exception("application terminated unexpectedly")
        raise
    finally:
        if instance is not None:
            stop()


if __name__ == "__main__":
    main()
