from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VideoFormatCandidate:
    format_spec: str
    merge_output_format: str | None
    estimated_size: int
    estimated_confident: bool
    quality_label: str
    compatibility: int
    direct_urls: tuple[str, ...]

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass(frozen=True)
class PendingRequest:
    created_at: float
    user_id: int
    chat_id: int
    reply_to_message_id: int
    url: str
    title: str
    video_candidates: tuple[VideoFormatCandidate, ...]
    audio_plan: dict[str, Any] | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class DownloadJob:
    job_id: str
    user_id: int
    chat_id: int
    reply_to_message_id: int
    status_message_id: int
    url: str
    title: str
    mode: str
    video_candidates: tuple[VideoFormatCandidate, ...]
    audio_plan: dict[str, Any] | None
    metadata: dict[str, Any]
    deadline: float


@dataclass
class ActiveJob:
    user_id: int
    chat_id: int
    status_message_id: int
    cancel_event: threading.Event
