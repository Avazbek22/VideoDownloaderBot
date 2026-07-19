from __future__ import annotations

import os

import pytest

from app.settings import load_settings


def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BOT_TOKEN",
        "LOGS_CHAT_ID",
        "OUTPUT_FOLDER",
        "LOGS_DIR",
        "MAX_FILESIZE",
        "WORKERS",
        "MAX_QUEUE",
        "UPLOAD_WORKERS",
        "JOB_TIMEOUT_SECONDS",
        "PENDING_TTL_SECONDS",
        "YTDLP_CONCURRENT_FRAGMENTS",
        "COOKIES_FILE",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_invalid_configuration_is_rejected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "123:test-token-value-abcdefghijklmnop")
    monkeypatch.setenv("WORKERS", "0")
    with pytest.raises(RuntimeError, match="WORKERS"):
        load_settings(tmp_path)


def test_operational_configuration(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "123:test-token-value-abcdefghijklmnop")
    monkeypatch.setenv("MAX_QUEUE", "7")
    settings = load_settings(tmp_path)
    assert settings.max_queue == 7
    assert settings.max_filesize == 50 * 1024 * 1024
    assert os.fspath(settings.output_dir).endswith(os.fspath(tmp_path / "data" / "downloads"))
