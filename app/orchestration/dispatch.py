"""Engine selection for the request path.

``ENGINE=fsm`` (the default) is the original behaviour, unchanged.
``ENGINE=langgraph`` runs the message path on ``LangGraphOrchestrator`` and
keeps everything the graph deliberately does not model on the FSM pipeline, so
flipping the switch never silently drops a feature.

Two rules make this safe, and both are load-bearing:

* The fallback test only *reads* session state (``Engine.peek``). Calling
  ``Engine.handle_event`` to "check" a session would advance the FSM as a side
  effect and corrupt the very state it was inspecting.
* The request log is written here, once, for the graph path only. ``MoAPipeline``
  already logs its own path internally; logging both would double-count every
  request that falls back.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.fsm.state_machine import Event as FsmEvent
from app.middleware.request_logger import log_request
from app.models.events import MoAEvent
from app.pipeline import PipelineResult

logger = logging.getLogger("moa.orchestration.dispatch")


class EngineDispatcher:
    """Routes each request to the engine that actually models it."""

    def __init__(self, fsm: Any, graph: Any = None) -> None:
        self._fsm = fsm
        self._graph = graph

    @property
    def engine_name(self) -> str:
        return "langgraph" if self._graph is not None else "fsm"

    def describe(self) -> dict[str, str]:
        return {"engine": self.engine_name}

    def _session_metadata(self, session_id: str) -> dict[str, Any]:
        engine = getattr(self._fsm, "engine", None)
        peek = getattr(engine, "peek", None)
        if peek is None:
            return {}
        ctx = peek(session_id)
        return dict(getattr(ctx, "metadata", None) or {})

    def needs_fsm(self, event: MoAEvent) -> bool:
        """Four paths stay on the FSM; everything else may take the graph."""
        if self._graph is None:
            return True
        if (event.text or "").lstrip().startswith("/"):
            return True
        if event.event in (
            FsmEvent.RESET,
            FsmEvent.CANCEL,
            FsmEvent.SENSITIVE_DETECTED,
        ):
            return True
        metadata = self._session_metadata(event.session_id)
        return bool(metadata.get("sensitive_pending") or metadata.get("hitl_pending"))

    async def run(
        self,
        event: MoAEvent,
        *,
        channel: str,
        target: str,
        request: Any | None = None,
    ) -> PipelineResult:
        if self.needs_fsm(event):
            return await self._fsm.run(
                event, channel=channel, target=target, request=request
            )

        started = time.monotonic()
        try:
            result = await self._graph.run(event, channel=channel, target=target)
        except Exception:
            # A graph that dies on a request would otherwise take the whole
            # endpoint down; the FSM path is the proven one, so fall back once.
            logger.exception(
                "langgraph engine failed session=%s; retrying on FSM",
                event.session_id,
            )
            return await self._fsm.run(
                event, channel=channel, target=target, request=request
            )

        if request is not None:
            await self._log_request(request, event, result, started)
        return result

    async def _log_request(
        self,
        request: Any,
        event: MoAEvent,
        result: PipelineResult,
        started: float,
    ) -> None:
        await log_request(
            request,
            200,
            (time.monotonic() - started) * 1000,
            event.session_id,
            result.agent_name,
            result.intent,
            result.guard_action,
            event.text,
            result.text,
            policy_hits=result.policy_hits,
            llm_model=result.llm_model,
            cost_usd=result.cost_usd,
            llm_latency_ms=result.llm_latency_ms,
            fallback_used=result.fallback_used,
        )


__all__ = ["EngineDispatcher"]
