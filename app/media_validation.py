from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

MP4_COMPATIBLE_FORMATS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}


class MediaValidationError(RuntimeError):
    pass


def _probe(path: Path, timeout_seconds: float) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height:format=format_name",
                "-of",
                "json",
                os.fspath(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout_seconds),
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise MediaValidationError("ffprobe could not read the downloaded file") from exc
    if not isinstance(data, dict):
        raise MediaValidationError("ffprobe returned invalid metadata")
    return data


def validate_media_file(
    path: str | Path,
    mode: str,
    limit_bytes: int,
    *,
    timeout_seconds: float = 15,
) -> None:
    media_path = Path(path)
    try:
        size = media_path.stat().st_size
    except OSError as exc:
        raise MediaValidationError("downloaded file does not exist") from exc
    if size <= 0:
        raise MediaValidationError("downloaded file is empty")
    if size > limit_bytes:
        raise MediaValidationError("downloaded file exceeds the configured size limit")

    probe = _probe(media_path, timeout_seconds)
    streams = probe.get("streams")
    if not isinstance(streams, list):
        streams = []
    audio_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"]
    video_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"]

    if mode == "audio":
        if not audio_streams:
            raise MediaValidationError("downloaded file has no audio stream")
        return

    if not video_streams:
        raise MediaValidationError("downloaded file has no video stream")
    if mode == "doc":
        format_data = probe.get("format")
        if not isinstance(format_data, dict) or not str(format_data.get("format_name") or "").strip():
            raise MediaValidationError("downloaded container is unreadable")
        return

    video = video_streams[0]
    if not isinstance(video.get("width"), int) or video["width"] <= 0:
        raise MediaValidationError("video width is invalid")
    if not isinstance(video.get("height"), int) or video["height"] <= 0:
        raise MediaValidationError("video height is invalid")
    if str(video.get("codec_name") or "").lower() != "h264":
        raise MediaValidationError("video stream is not H.264")
    format_data = probe.get("format")
    format_names = (
        set(str(format_data.get("format_name") or "").lower().split(",")) if isinstance(format_data, dict) else set()
    )
    if not format_names.intersection(MP4_COMPATIBLE_FORMATS):
        raise MediaValidationError("video container is not MP4-compatible")
