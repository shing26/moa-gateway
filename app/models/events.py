from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.fsm.state_machine import Event


@dataclass(frozen=True)
class PlatformEvent:
    platform: str
    message_id: str
    session_id: str
    user_id: str
    payload: dict[str, Any]
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class MoAEvent:
    trace_id: str
    event: Event
    session_id: str
    text: str
    context: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ContextSnapshot:
    trace_id: str
    session_id: str
    state: str
    envelope: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def new_trace_id() -> str:
    import base64
    return base64.urlsafe_b64encode(uuid4().bytes).rstrip(b"=").decode("ascii")
