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

    def to_audit_dict(self) -> dict[str, Any]:
        """审计条目的**唯一**序列化出口，WAL 与 ES 都从这里取字段。

        此前两个 sink 各自手写一份字段清单，于是新加的字段（``route_fallback`` /
        ``tool_calls`` / ``context_budget`` / ``retry_count`` / ``hitl_kind`` …）
        只进了内存对象，落盘时被静默丢弃——而 README 已声称它们可查。字段集合
        从此只在这里维护；``tests/unit/test_audit_field_coverage.py`` 会强制每个
        ``AuditEntry`` 字段与每个 ``extra`` 键都出现在输出里。

        输出是**扁平**的（``extra`` 的键平铺到顶层）：这是审计 JSONL 的既定约定，
        ``app/services/audit_stats.py`` 与 ``scripts/collect_hitl_feedback.py``
        都按顶层键读取。
        """
        typed: dict[str, Any] = {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "intent": self.intent,
            "timestamp": self.timestamp.isoformat(),
            "agent_output": self.agent_output,
            "agent_output_len": len(self.agent_output),
            "eval_score": self.eval_score,
            "eval_issues": list(self.eval_issues),
            "guard_action": self.guard_action,
            "guard_reason": self.guard_reason,
            "policy_hits": list(self.policy_hits or ()),
            "violation": self.violation,
            "hitl_decision": self.hitl_decision,
            "hitl_duration_ms": self.hitl_duration_ms,
        }
        # extra 先铺开、类型化字段后覆盖：同名时以 dataclass 上的值为准（确定性）。
        return {**self.extra, **typed}
