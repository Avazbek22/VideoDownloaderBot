from __future__ import annotations

import errno
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


class DownloadSizeExceeded(RuntimeError):
    """The current download attempt exceeded the configured byte budget."""


class DownloadStorageError(RuntimeError):
    """The download workspace ran out of storage."""


def _positive_bytes(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return math.ceil(value)


_TRANSIENT_DOWNLOAD_SUFFIX = re.compile(r"\.part(?:-frag\d+)?$", re.IGNORECASE)


def _stream_key(status: dict[str, Any]) -> str:
    filename = status.get("filename") or status.get("tmpfilename")
    if filename:
        return _TRANSIENT_DOWNLOAD_SUFFIX.sub("", str(filename))
    info = status.get("info_dict")
    format_id = info.get("format_id") if isinstance(info, dict) else None
    return str(format_id or "default")


def enforce_download_limit(status: dict[str, Any], limit_bytes: int) -> None:
    """Abort an attempt as soon as yt-dlp reports that it cannot fit."""
    downloaded = _positive_bytes(status.get("downloaded_bytes"))
    if downloaded is not None and downloaded > limit_bytes:
        raise DownloadSizeExceeded("downloaded bytes exceeded the configured size limit")

    for field_name in ("total_bytes", "total_bytes_estimate"):
        total = _positive_bytes(status.get(field_name))
        if total is not None and total > limit_bytes:
            raise DownloadSizeExceeded(f"{field_name} exceeded the configured size limit")


@dataclass
class DownloadByteLimiter:
    """Track the cumulative high-water mark of every stream in one attempt."""

    limit_bytes: int
    _downloaded_by_stream: dict[str, int] = field(default_factory=dict)

    def check(self, status: dict[str, Any]) -> None:
        enforce_download_limit(status, self.limit_bytes)
        downloaded = _positive_bytes(status.get("downloaded_bytes"))
        if downloaded is None:
            return
        stream_key = _stream_key(status)
        self._downloaded_by_stream[stream_key] = max(
            downloaded,
            self._downloaded_by_stream.get(stream_key, 0),
        )
        if sum(self._downloaded_by_stream.values()) > self.limit_bytes:
            raise DownloadSizeExceeded("cumulative downloaded bytes exceeded the configured size limit")


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def is_download_size_error(error: BaseException) -> bool:
    return any(
        isinstance(item, DownloadSizeExceeded) or "exceeded the configured size limit" in str(item).lower()
        for item in _exception_chain(error)
    )


def is_enospc_error(error: BaseException) -> bool:
    for item in _exception_chain(error):
        if isinstance(item, OSError) and item.errno == errno.ENOSPC:
            return True
        message = str(item).lower()
        if "no space left on device" in message or "[errno 28]" in message:
            return True
    return False


def raise_if_terminal_storage_error(error: BaseException) -> None:
    if isinstance(error, DownloadStorageError):
        raise error
    if is_enospc_error(error):
        raise DownloadStorageError("download workspace has no free space") from error
