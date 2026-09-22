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
    # None = 这条请求没走评测（控制指令 / 敏感挂起 / 路由前早退等），
    # 与 0.0（评测跑了且判定为 AST 危险）是两件事。此前两者都用 0.0 表示，
    # 于是"接线断开"和"真的危险"在数据里分不开——见 2026-09-22 的修补记录。
    eval_score: float | None = None
    eval_issues: tuple[str, ...] = ()
    guard_action: str = ""
    guard_reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] = field(default_factory=dict)
    policy_hits: tuple[str, ...] = ()
    violation: str = ""
    hitl_decision: str = ""
    hitl_duration_ms: float = 0.0
