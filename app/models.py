from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PendingRequest:
    created_at: float
    user_id: int
    chat_id: int
    reply_to_message_id: int
    url: str
    title: str
    video_plan: dict[str, Any] | None
    audio_plan: dict[str, Any] | None


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
    plan: dict[str, Any]
    deadline: float


@dataclass
class ActiveJob:
    user_id: int
    chat_id: int
    status_message_id: int
    cancel_event: threading.Event
