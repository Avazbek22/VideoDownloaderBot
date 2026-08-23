from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import main
from app.settings import Settings


def test_youtube_metadata_skips_client_without_downloadable_video(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings = Settings(**{**settings.__dict__, "ytdlp_youtube_player_clients": "default,android,ios"})
    main.SETTINGS = settings
    clients_seen: list[str | None] = []

    def fake_get_video_meta(_url, **kwargs):
        client = kwargs.get("youtube_player_client")
        clients_seen.append(client)
        if client is None:
            return {"formats": []}
        return {"formats": [{"format_id": "18", "vcodec": "avc1.42001e", "acodec": "mp4a"}]}

    monkeypatch.setattr(main, "_get_video_meta", fake_get_video_meta)
    monkeypatch.setattr(main, "_validate_metadata_urls", lambda _metadata: None)

    metadata = main._get_video_meta_with_hidden_retries("https://www.youtube.com/watch?v=example")

    assert metadata["formats"][0]["format_id"] == "18"
    assert clients_seen == [None, "android"]


def _settings(tmp_path: Path, *, workers: int = 1, timeout: int = 5) -> Settings:
    return Settings(
        token="123:test-token-value-abcdefghijklmnop",
        logs_chat_id=None,
        output_dir=tmp_path / "data" / "downloads",
        logs_dir=tmp_path / "logs",
        max_filesize=50 * 1024 * 1024,
        workers=1,
        max_queue=2,
        upload_workers=1,
        job_timeout_seconds=60,
        pending_ttl_seconds=30,
        concurrent_fragments=1,
        cookies_file=None,
        log_level="INFO",
        metadata_workers=workers,
        metadata_timeout_seconds=timeout,
    )


def test_metadata_semaphore_rejects_work_when_capacity_is_full(tmp_path) -> None:
    main.SETTINGS = _settings(tmp_path)
    main.metadata_slots = threading.BoundedSemaphore(1)
    main.metadata_executor = ThreadPoolExecutor(max_workers=1)
    assert main.metadata_slots.acquire(blocking=False)
    try:
        with pytest.raises(main.MetadataBusy):
            main._run_metadata_operation("https://example.com/video")
    finally:
        main.metadata_slots.release()
        main.metadata_executor.shutdown(wait=True)
        main.metadata_executor = None


def test_metadata_timeout_returns_without_creating_unbounded_work(tmp_path, monkeypatch) -> None:
    main.SETTINGS = _settings(tmp_path, timeout=5)
    main.metadata_slots = threading.BoundedSemaphore(1)
    main.metadata_executor = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    monkeypatch.setattr(main, "_get_video_meta_with_hidden_retries", lambda _url: release.wait(10) or {})
    monkeypatch.setattr(main, "_settings", lambda: type("S", (), {"metadata_timeout_seconds": 0.01})())
    started = time.monotonic()
    try:
        with pytest.raises(main.MetadataTimedOut):
            main._run_metadata_operation("https://example.com/video")
        assert time.monotonic() - started < 1
        with pytest.raises(main.MetadataBusy):
            main._run_metadata_operation("https://example.com/other")
    finally:
        release.set()
        main.metadata_executor.shutdown(wait=True)
        main.metadata_executor = None
