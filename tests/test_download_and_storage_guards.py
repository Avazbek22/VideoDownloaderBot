from __future__ import annotations

import errno

import pytest

from app.download_guard import (
    DownloadByteLimiter,
    DownloadSizeExceeded,
    DownloadStorageError,
    enforce_download_limit,
    is_download_size_error,
    is_enospc_error,
    raise_if_terminal_storage_error,
)
from app.storage_guard import required_workspace_bytes, validate_workspace_location


def test_download_guard_uses_downloaded_bytes_as_the_hard_limit() -> None:
    with pytest.raises(DownloadSizeExceeded, match="downloaded bytes"):
        enforce_download_limit({"downloaded_bytes": 101}, 100)


@pytest.mark.parametrize("field", ["total_bytes", "total_bytes_estimate"])
def test_download_guard_rejects_reported_totals_above_limit(field: str) -> None:
    with pytest.raises(DownloadSizeExceeded, match=field):
        enforce_download_limit({"downloaded_bytes": 1, field: 101}, 100)


def test_download_guard_allows_exact_limit() -> None:
    enforce_download_limit(
        {"downloaded_bytes": 100, "total_bytes": 100, "total_bytes_estimate": 100},
        100,
    )


def test_download_guard_does_not_truncate_fractional_byte_counts() -> None:
    with pytest.raises(DownloadSizeExceeded):
        enforce_download_limit({"downloaded_bytes": 100.1}, 100)


def test_download_guard_caps_combined_video_and_audio_streams() -> None:
    limiter = DownloadByteLimiter(100)
    limiter.check({"downloaded_bytes": 60, "info_dict": {"format_id": "video"}})
    with pytest.raises(DownloadSizeExceeded, match="cumulative"):
        limiter.check({"downloaded_bytes": 41, "info_dict": {"format_id": "audio"}})


def test_download_guard_distinguishes_component_files_with_combined_format_id() -> None:
    limiter = DownloadByteLimiter(100)
    limiter.check(
        {
            "downloaded_bytes": 60,
            "filename": "/workspace/item.f137.mp4.part",
            "info_dict": {"format_id": "137+140"},
        }
    )
    with pytest.raises(DownloadSizeExceeded, match="cumulative"):
        limiter.check(
            {
                "downloaded_bytes": 41,
                "filename": "/workspace/item.f140.m4a.part",
                "info_dict": {"format_id": "137+140"},
            }
        )


def test_download_guard_normalizes_part_filename_for_one_stream() -> None:
    limiter = DownloadByteLimiter(100)
    limiter.check({"downloaded_bytes": 60, "tmpfilename": "/workspace/item.mp4.part"})
    limiter.check({"downloaded_bytes": 100, "filename": "/workspace/item.mp4"})


def test_wrapped_size_and_enospc_errors_are_recognized() -> None:
    try:
        raise DownloadSizeExceeded("downloaded bytes exceeded the configured size limit")
    except DownloadSizeExceeded as cause:
        wrapped_size = RuntimeError("yt-dlp wrapper")
        wrapped_size.__cause__ = cause
    assert is_download_size_error(wrapped_size)

    try:
        raise OSError(errno.ENOSPC, "No space left on device")
    except OSError as cause:
        wrapped_storage = RuntimeError("yt-dlp wrapper")
        wrapped_storage.__cause__ = cause
    assert is_enospc_error(wrapped_storage)
    with pytest.raises(DownloadStorageError):
        raise_if_terminal_storage_error(wrapped_storage)


def test_container_workspace_must_be_under_bind_mounted_data(tmp_path) -> None:
    validate_workspace_location(tmp_path / "data" / "downloads", containerized=False)
    validate_workspace_location(
        tmp_path / "data" / "downloads",
        containerized=True,
        data_root=tmp_path / "data",
    )
    with pytest.raises(RuntimeError, match="/app/data"):
        validate_workspace_location(
            tmp_path / "tmp" / "downloads",
            containerized=True,
            data_root=tmp_path / "data",
        )


def test_workspace_reserves_merge_space_for_every_worker() -> None:
    assert required_workspace_bytes(50, workers=2) == 300
