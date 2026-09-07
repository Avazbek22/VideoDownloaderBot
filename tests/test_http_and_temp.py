from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

import main
from app.download_utils import find_file_by_prefix
from app.http_utils import telegram_upload_session
from app.temp_files import cleanup_job_directory, cleanup_stale_directories


def test_upload_session_has_no_automatic_post_retry() -> None:
    session = telegram_upload_session()
    retry = session.get_adapter("https://").max_retries
    assert retry.total == 0
    assert retry.connect == 0
    assert retry.read == 0
    assert not retry.allowed_methods or "POST" not in retry.allowed_methods
    session.close()


def test_upload_response_parsing() -> None:
    assert main._parse_upload_response(SimpleNamespace(json=lambda: {"ok": True}, status_code=200))["ok"]
    with pytest.raises(RuntimeError, match="Telegram API error"):
        main._parse_upload_response(SimpleNamespace(json=lambda: {"ok": False, "description": "bad"}, status_code=400))


def test_temporary_file_cleanup(tmp_path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    for name in ("video.part", "video.ytdl", "merge.mp4"):
        (job / name).write_bytes(b"x")
    cleanup_job_directory(job)
    assert not job.exists()

    stale = tmp_path / "stale"
    stale.mkdir()
    os.utime(stale, (time.time() - 1000, time.time() - 1000))
    assert cleanup_stale_directories(tmp_path, 100) == 1
    assert not stale.exists()


def test_download_discovery_ignores_partial_and_component_files(tmp_path) -> None:
    (tmp_path / "item.part").write_bytes(b"partial")
    (tmp_path / "item.f137.mp4").write_bytes(b"video component")
    final = tmp_path / "item.mp4"
    final.write_bytes(b"final")

    assert find_file_by_prefix(os.fspath(tmp_path), "item") == os.fspath(final)
