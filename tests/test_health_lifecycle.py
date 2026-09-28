from __future__ import annotations

import threading
import time

import pytest

import main
from app import healthcheck


def test_healthcheck_uses_only_tmp_marker(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "videodownloaderbot.healthy"
    monkeypatch.setenv("HEALTH_MARKER", str(marker))
    assert healthcheck.main() == 1
    marker.touch()
    assert healthcheck.main() == 0
    old = time.time() - healthcheck.MAX_AGE_SECONDS - 1
    marker.touch()
    import os

    os.utime(marker, (old, old))
    assert healthcheck.main() == 1


def test_stop_removes_health_marker(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "health"
    monkeypatch.setattr(main, "HEALTH_MARKER", marker)
    marker.touch()
    main.bot = None
    main.worker_threads.clear()
    main.maintenance_thread = None
    main.metadata_executor = None
    main.stop()
    assert not marker.exists()


def test_dead_worker_marks_application_unhealthy(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "health"
    monkeypatch.setattr(main, "HEALTH_MARKER", marker)
    main.stop_event.clear()
    main.maintenance_finished.clear()
    main.fatal_lifecycle_error.clear()
    main.worker_failure_alerted.clear()
    main.bot = None
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    main.worker_threads[:] = [dead]
    alerts: list[str] = []
    monkeypatch.setattr(main, "_notify_operator_critical", alerts.append)

    assert main._heartbeat() is False
    assert main.stop_event.is_set()
    assert main.fatal_lifecycle_error.is_set()
    assert not marker.exists()
    assert len(alerts) == 1


def test_stale_polling_reports_unhealthy_and_recovers(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "health"
    monkeypatch.setattr(main, "HEALTH_MARKER", marker)
    main.stop_event.clear()
    main.maintenance_finished.clear()
    main.poll_stale.clear()
    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    worker.start()
    main.worker_threads[:] = [worker]
    try:
        monkeypatch.setattr(main, "last_successful_poll", time.monotonic() - 1000)
        assert main._heartbeat() is True
        assert not marker.exists()
        assert not main.stop_event.is_set()

        monkeypatch.setattr(main, "last_successful_poll", time.monotonic())
        assert main._heartbeat() is True
        assert marker.exists()
        assert not main.poll_stale.is_set()
    finally:
        release.set()
        worker.join()
        main.worker_threads.clear()


def test_successful_poll_is_recorded(monkeypatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.fail = False

        def get_updates(self, *args, **kwargs):
            if self.fail:
                raise ConnectionError("Conflict: terminated by other getUpdates request")
            return []

    fake = FakeBot()
    monkeypatch.setattr(main, "last_successful_poll", 0.0)
    main._record_successful_polls(fake)
    fake.fail = True
    with pytest.raises(ConnectionError):
        fake.get_updates(offset=1)
    assert main.last_successful_poll == 0.0
    fake.fail = False
    assert fake.get_updates(offset=1) == []
    assert main.last_successful_poll > 0.0
