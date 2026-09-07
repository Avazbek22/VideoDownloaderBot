from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def cleanup_directory_contents(path: Path, *, strict: bool = False) -> None:
    if not path.exists():
        return
    try:
        children = list(path.iterdir())
    except OSError:
        LOGGER.exception("attempt cleanup enumeration failed path=%s", path)
        if strict:
            raise
        return
    first_error: OSError | None = None
    for child in children:
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        except OSError as exc:
            first_error = first_error or exc
            LOGGER.exception("attempt cleanup failed path=%s", child)
    if strict and first_error is not None:
        raise first_error


def cleanup_job_directory(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError:
        LOGGER.exception("temporary cleanup failed path=%s", path)


def cleanup_stale_directories(root: Path, older_than_seconds: int) -> int:
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - older_than_seconds
    removed = 0
    for path in root.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                cleanup_job_directory(path)
                removed += int(not path.exists())
            elif path.is_file() and path.suffix.lower() in {".part", ".ytdl", ".tmp", ".temp"}:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            LOGGER.exception("stale temporary cleanup failed path=%s", path)
    return removed
