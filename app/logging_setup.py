from __future__ import annotations

import logging
import re
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class RedactingFormatter(logging.Formatter):
    _url = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
    _bot_token = re.compile(r"(?<![A-Za-z0-9_-])\d{5,}:[A-Za-z0-9_-]{20,}")

    def format(self, record: logging.LogRecord) -> str:
        message = self._url.sub("<url-redacted>", super().format(record))
        return self._bot_token.sub("<bot-token-redacted>", message)


def configure_logging(logs_dir: Path, level: str) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logging.Formatter.converter = time.gmtime
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()
    formatter = RedactingFormatter(
        "%(asctime)sZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    rotating = TimedRotatingFileHandler(
        logs_dir / "bot.log", when="midnight", interval=1, backupCount=60, encoding="utf-8", utc=True
    )
    rotating.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(rotating)
    return logging.getLogger("video_downloader_bot")
