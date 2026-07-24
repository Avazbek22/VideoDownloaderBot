from __future__ import annotations

import os

import pytest

from app.planner import apply_youtube_runtime_opts
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
        "YTDLP_JS_RUNTIMES",
        "YTDLP_REMOTE_COMPONENTS",
        "YTDLP_INSTAGRAM_IMPERSONATE",
        "YTDLP_INSTAGRAM_RETRIES",
        "YTDLP_INSTAGRAM_FRAGMENT_RETRIES",
        "YTDLP_INSTAGRAM_SOCKET_TIMEOUT",
        "METADATA_WORKERS",
        "METADATA_TIMEOUT_SECONDS",
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


def test_ytdlp_and_metadata_settings_are_loaded_from_local_env(tmp_path, monkeypatch) -> None:
    _clean(monkeypatch)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "BOT_TOKEN=123:test-token-value-abcdefghijklmnop",
                "YTDLP_JS_RUNTIMES=node:/custom/node",
                "YTDLP_REMOTE_COMPONENTS=",
                "YTDLP_INSTAGRAM_IMPERSONATE=",
                "YTDLP_INSTAGRAM_RETRIES=3",
                "YTDLP_INSTAGRAM_FRAGMENT_RETRIES=4",
                "YTDLP_INSTAGRAM_SOCKET_TIMEOUT=25",
                "METADATA_WORKERS=3",
                "METADATA_TIMEOUT_SECONDS=45",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)

    assert settings.ytdlp_js_runtimes == "node:/custom/node"
    assert settings.ytdlp_remote_components == ""
    assert settings.ytdlp_instagram_impersonate is None
    assert settings.ytdlp_instagram_retries == 3
    assert settings.ytdlp_instagram_fragment_retries == 4
    assert settings.ytdlp_instagram_socket_timeout == 25
    assert settings.metadata_workers == 3
    assert settings.metadata_timeout_seconds == 45
    options = apply_youtube_runtime_opts(
        {},
        "https://www.youtube.com/watch?v=example",
        settings.ytdlp_js_runtimes,
        settings.ytdlp_remote_components,
    )
    assert "remote_components" not in options


def test_invalid_metadata_configuration_is_rejected(tmp_path, monkeypatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setenv("BOT_TOKEN", "123:test-token-value-abcdefghijklmnop")
    monkeypatch.setenv("METADATA_WORKERS", "0")
    with pytest.raises(RuntimeError, match="METADATA_WORKERS"):
        load_settings(tmp_path)
