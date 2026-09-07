from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    token: str
    logs_chat_id: int | None
    output_dir: Path
    logs_dir: Path
    max_filesize: int
    workers: int
    max_queue: int
    upload_workers: int
    job_timeout_seconds: int
    pending_ttl_seconds: int
    concurrent_fragments: int
    cookies_file: Path | None
    log_level: str
    ytdlp_js_runtimes: str = "node"
    ytdlp_remote_components: str = ""
    ytdlp_instagram_impersonate: str | None = "chrome"
    ytdlp_instagram_retries: int = 8
    ytdlp_instagram_fragment_retries: int = 8
    ytdlp_instagram_socket_timeout: int = 30
    ytdlp_youtube_player_clients: str = "default,android,ios"
    metadata_workers: int = 2
    metadata_timeout_seconds: int = 60
    ytdlp_generic_impersonate: str | None = "chrome"


def load_settings(base_dir: Path | None = None) -> Settings:
    base = (base_dir or Path(__file__).resolve().parents[1]).resolve()
    _load_dotenv(base / ".env")

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    if len(token) > 256 or ":" not in token:
        raise RuntimeError("BOT_TOKEN has an invalid format")

    logs_raw = (os.getenv("LOGS_CHAT_ID") or "").strip()
    try:
        logs_chat_id = int(logs_raw) if logs_raw else None
    except ValueError as exc:
        raise RuntimeError("LOGS_CHAT_ID must be an integer") from exc

    output_dir = Path(os.getenv("OUTPUT_FOLDER") or base / "data" / "downloads").expanduser().resolve()
    logs_dir = Path(os.getenv("LOGS_DIR") or base / "logs").expanduser().resolve()
    cookies_raw = (os.getenv("COOKIES_FILE") or "").strip()
    log_level = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise RuntimeError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    js_runtimes = (os.getenv("YTDLP_JS_RUNTIMES") or "node").strip()
    remote_components = (os.getenv("YTDLP_REMOTE_COMPONENTS") or "").strip()
    instagram_impersonate = (os.getenv("YTDLP_INSTAGRAM_IMPERSONATE") or "").strip() or None
    generic_impersonate = (os.getenv("YTDLP_GENERIC_IMPERSONATE") or "chrome").strip() or None
    youtube_player_clients = (os.getenv("YTDLP_YOUTUBE_PLAYER_CLIENTS") or "default,android,ios").strip()
    if len(youtube_player_clients) > 256:
        raise RuntimeError("YTDLP_YOUTUBE_PLAYER_CLIENTS is too long")

    return Settings(
        token=token,
        logs_chat_id=logs_chat_id,
        output_dir=output_dir,
        logs_dir=logs_dir,
        max_filesize=_integer("MAX_FILESIZE", 50 * 1024 * 1024, 1024 * 1024, 2 * 1024**3),
        workers=_integer("WORKERS", 2, 1, 16),
        max_queue=_integer("MAX_QUEUE", 200, 1, 10_000),
        upload_workers=_integer("UPLOAD_WORKERS", 2, 1, 16),
        job_timeout_seconds=_integer("JOB_TIMEOUT_SECONDS", 900, 30, 86_400),
        pending_ttl_seconds=_integer("PENDING_TTL_SECONDS", 600, 30, 86_400),
        concurrent_fragments=_integer("YTDLP_CONCURRENT_FRAGMENTS", 4, 1, 32),
        cookies_file=Path(cookies_raw).expanduser().resolve() if cookies_raw else None,
        log_level=log_level,
        ytdlp_js_runtimes=js_runtimes,
        ytdlp_remote_components=remote_components,
        ytdlp_instagram_impersonate=instagram_impersonate,
        ytdlp_instagram_retries=_integer("YTDLP_INSTAGRAM_RETRIES", 8, 0, 50),
        ytdlp_instagram_fragment_retries=_integer("YTDLP_INSTAGRAM_FRAGMENT_RETRIES", 8, 0, 50),
        ytdlp_instagram_socket_timeout=_integer("YTDLP_INSTAGRAM_SOCKET_TIMEOUT", 30, 1, 300),
        ytdlp_youtube_player_clients=youtube_player_clients,
        metadata_workers=_integer("METADATA_WORKERS", 2, 1, 16),
        metadata_timeout_seconds=_integer("METADATA_TIMEOUT_SECONDS", 60, 5, 600),
        ytdlp_generic_impersonate=generic_impersonate,
    )
