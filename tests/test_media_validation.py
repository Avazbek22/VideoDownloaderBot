from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.media_validation import MediaValidationError, validate_media_file


def _ffprobe(monkeypatch, *, streams: list[dict], format_name: str = "mov,mp4,m4a,3gp,3g2,mj2") -> None:
    result = SimpleNamespace(
        stdout=json.dumps({"streams": streams, "format": {"format_name": format_name}}),
        stderr="",
    )
    monkeypatch.setattr("app.media_validation.subprocess.run", lambda *_args, **_kwargs: result)


def _file(tmp_path: Path) -> Path:
    path = tmp_path / "download.mp4"
    path.write_bytes(b"media")
    return path


def test_video_rejects_audio_only_file(tmp_path, monkeypatch) -> None:
    _ffprobe(monkeypatch, streams=[{"codec_type": "audio", "codec_name": "aac"}])

    with pytest.raises(MediaValidationError, match="video stream"):
        validate_media_file(_file(tmp_path), "video", 1024)


def test_video_rejects_non_h264_file(tmp_path, monkeypatch) -> None:
    _ffprobe(
        monkeypatch,
        streams=[{"codec_type": "video", "codec_name": "vp9", "width": 1280, "height": 720}],
    )

    with pytest.raises(MediaValidationError, match="H.264"):
        validate_media_file(_file(tmp_path), "video", 1024)


def test_document_accepts_readable_non_h264_video(tmp_path, monkeypatch) -> None:
    _ffprobe(
        monkeypatch,
        streams=[{"codec_type": "video", "codec_name": "vp9", "width": 1280, "height": 720}],
        format_name="matroska,webm",
    )

    validate_media_file(_file(tmp_path), "doc", 1024)


def test_audio_requires_audio_stream(tmp_path, monkeypatch) -> None:
    _ffprobe(
        monkeypatch,
        streams=[{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}],
    )

    with pytest.raises(MediaValidationError, match="audio stream"):
        validate_media_file(_file(tmp_path), "audio", 1024)
