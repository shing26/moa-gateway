from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from app.audit.models import AuditEntry
from app.audit.wal import AsyncWal, LogConfig
from app.models.events import new_trace_id

logger = logging.getLogger("moa.middleware.request_logger")

_wal = AsyncWal(_config=LogConfig(directory="logs", retention_days=90))

# 请求作用域的审计 trace：一次请求内的所有审计条目（含双引擎与后台任务）
# 共享同一个 trace，否则 review 与后续 hitl_approve/reject 分属两条互不
# 关联的记录，人工决策无法回流、也无法按 trace 复盘（此前每条都用
# new_trace_id()，审计虽在但连不起来）。
_current_trace: ContextVar[str] = ContextVar("moa_audit_trace", default="")

# 请求作用域的上下文预算决策（由 pipeline/graph 在裁剪后绑定），写进审计
# extra，使"这次请求裁掉了多少上下文"可按 trace 查询，而不只是日志一行。
_context_stats: ContextVar[dict[str, Any] | None] = ContextVar("moa_context_stats", default=None)


def bind_trace(trace_id: str) -> None:
    """把当前请求（或后台任务）的 trace 绑定到审计上下文。"""
    _current_trace.set(trace_id or "")


def bind_context_stats(stats: dict[str, Any] | None) -> None:
    """把当前请求的上下文预算决策绑定到审计上下文。"""
    _context_stats.set(stats or None)


async def log_request(
    request: Any,
    status_code: int,
    duration_ms: float,
    session_id: str = "",
    agent_name: str = "",
    intent: str = "",
    guard_action: str = "",
    input_text: str = "",
    output_text: str = "",
    policy_hits: tuple[str, ...] = (),
    hitl_decision: str = "",
    hitl_duration_ms: float = 0.0,
    llm_model: str = "",
    cost_usd: float = 0.0,
    llm_latency_ms: float = 0.0,
    fallback_used: str = "",
) -> None:
    input_preview = input_text.strip()[:500]
    output_preview = output_text.strip()[:2000]
    extra: dict[str, Any] = {
        "method": request.method if hasattr(request, "method") else "",
        "path": str(request.url) if hasattr(request, "url") else "",
        "status": status_code,
        "duration_ms": round(duration_ms, 1),
        "input_preview": input_preview,
        "output_preview": output_preview,
        "policy_hits": policy_hits,
        "hitl_decision": hitl_decision,
        "hitl_duration_ms": hitl_duration_ms,
        "llm_model": llm_model,
        "cost_usd": cost_usd,
        "llm_latency_ms": llm_latency_ms,
        "fallback_used": fallback_used,
    }
    context_stats = _context_stats.get()
    if context_stats:
        extra["context_budget"] = context_stats
    entry = AuditEntry(
        trace_id=_current_trace.get() or new_trace_id(),
        session_id=session_id or "unknown",
        agent_name=agent_name,
        agent_output=output_preview,
        intent=intent or "unknown",
        eval_score=0.0,
        guard_action=guard_action,
        policy_hits=policy_hits,
        violation=policy_hits[0] if policy_hits else "",
        hitl_decision=hitl_decision,
        hitl_duration_ms=hitl_duration_ms,
        extra=extra,
    )
    await _wal.append(entry)
    try:
        from app.deps import es_writer

        if es_writer is not None:
            await es_writer.write(entry)
    except Exception:
        logger.warning("es audit write failed", exc_info=True)
