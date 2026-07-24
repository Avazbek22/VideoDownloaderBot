from __future__ import annotations

import threading
import time

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
