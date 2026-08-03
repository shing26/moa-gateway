from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.audit.models import AuditEntry
from app.audit.wal import AsyncWal, LogConfig
from app.models.events import new_trace_id

logger = logging.getLogger("moa.middleware.request_logger")

_wal = AsyncWal(_config=LogConfig(directory="logs", retention_days=90))


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
) -> None:
    input_preview = input_text.strip()[:500]
    output_preview = output_text.strip()[:2000]
    entry = AuditEntry(
        trace_id=new_trace_id(),
        session_id=session_id or "unknown",
        agent_name=agent_name,
        agent_output=output_preview,
        intent=intent or "unknown",
        eval_score=0.0,
        guard_action=guard_action,
        extra={
            "method": request.method if hasattr(request, "method") else "",
            "path": str(request.url) if hasattr(request, "url") else "",
            "status": status_code,
            "duration_ms": round(duration_ms, 1),
            "input_preview": input_preview,
            "output_preview": output_preview,
        },
    )
    await _wal.append(entry)
