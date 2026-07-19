from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import main
from app.models import ActiveJob, PendingRequest
from app.settings import Settings


def settings(tmp_path: Path, max_queue: int = 2) -> Settings:
    return Settings(
        token="123:test-token-value-abcdefghijklmnop",
        logs_chat_id=None,
        output_dir=tmp_path / "data" / "downloads",
        logs_dir=tmp_path / "logs",
        max_filesize=50 * 1024 * 1024,
        workers=1,
        max_queue=max_queue,
        upload_workers=1,
        job_timeout_seconds=60,
        pending_ttl_seconds=30,
        concurrent_fragments=1,
        cookies_file=None,
        log_level="INFO",
    )


def call(user_id: int = 7):
    return SimpleNamespace(
        id="callback",
        data="dl|video|request",
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(message_id=99),
    )


def prepare_request(tmp_path, monkeypatch, max_queue=2):
    main.SETTINGS = settings(tmp_path, max_queue)
    main.PENDING_TTL_SEC = 30
    main.jobs_q = queue.Queue(maxsize=max_queue)
    main.pending_requests.clear()
    main.active_jobs.clear()
    main.cancel_events.clear()
    main.pending_requests["request"] = PendingRequest(
        time.time(),
        7,
        10,
        11,
        "https://example.com/video",
        "Title",
        {"format_spec": "18"},
        None,
    )
    monkeypatch.setattr(main, "_safe_answer_callback", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_safe_edit", lambda *_a, **_kw: None)


def test_callback_double_click_creates_one_job(tmp_path, monkeypatch) -> None:
    prepare_request(tmp_path, monkeypatch)
    main.on_download_choice(call())
    main.on_download_choice(call())
    assert main.jobs_q.qsize() == 1


def test_queue_full_does_not_leave_active_job(tmp_path, monkeypatch) -> None:
    prepare_request(tmp_path, monkeypatch, max_queue=1)
    main.jobs_q.put_nowait(None)
    main.on_download_choice(call())
    assert not main.active_jobs
    assert not main.cancel_events


def test_pending_ttl_cleanup(tmp_path) -> None:
    main.PENDING_TTL_SEC = 30
    main.pending_requests.clear()
    main.pending_requests["old"] = PendingRequest(
        time.time() - 31,
        1,
        1,
        1,
        "https://example.com",
        "Title",
        None,
        None,
    )
    main._cleanup_pending()
    assert "old" not in main.pending_requests


def test_cancellation_sets_event(monkeypatch) -> None:
    event = threading.Event()
    main.active_jobs["job"] = ActiveJob(7, 10, 11, event)
    main.cancel_events["job"] = event
    monkeypatch.setattr(main, "_safe_answer_callback", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "_safe_delete", lambda *_a, **_kw: None)
    main.on_cancel(SimpleNamespace(id="c", data="cnl|job", from_user=SimpleNamespace(id=7)))
    assert event.is_set()


def test_graceful_stop_joins_worker() -> None:
    fake = SimpleNamespace(stop_bot=lambda: None)
    main.bot = fake
    main.jobs_q = queue.Queue(maxsize=1)
    main.stop_event.clear()
    worker = threading.Thread(target=main._worker_loop)
    worker.start()
    main.worker_threads[:] = [worker]
    main.maintenance_thread = None
    main.stop()
    assert main.stop_event.is_set()
    assert not worker.is_alive()
