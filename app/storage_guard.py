from __future__ import annotations

import shutil
from pathlib import Path

from app.download_guard import DownloadStorageError

CONTAINER_DATA_ROOT = Path("/app/data")
CONTAINER_MARKER = Path("/.dockerenv")
WORKSPACE_HEADROOM_FACTOR = 3


def validate_workspace_location(
    output_dir: Path,
    *,
    containerized: bool | None = None,
    data_root: Path = CONTAINER_DATA_ROOT,
) -> None:
    if containerized is None:
        containerized = CONTAINER_MARKER.exists()
    if not containerized:
        return
    resolved_output = output_dir.resolve()
    resolved_root = data_root.resolve()
    if not resolved_output.is_relative_to(resolved_root):
        raise RuntimeError("container download workspace must be located under /app/data")


def required_workspace_bytes(max_file_bytes: int, workers: int = 1) -> int:
    return max_file_bytes * max(1, workers) * WORKSPACE_HEADROOM_FACTOR


def ensure_workspace_capacity(path: Path, required_bytes: int) -> None:
    try:
        free_bytes = shutil.disk_usage(path).free
    except OSError as exc:
        raise DownloadStorageError("download workspace capacity cannot be determined") from exc
    if free_bytes < required_bytes:
        raise DownloadStorageError("download workspace has insufficient free space")
