from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class AuditEntry:
    trace_id: str
    session_id: str
    agent_name: str
    agent_output: str
    intent: str
    eval_score: float
    eval_issues: tuple[str, ...] = ()
    guard_action: str = ""
    guard_reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] = field(default_factory=dict)
