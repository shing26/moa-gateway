from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from app.fsm.state_machine import Event, State, next_state, StateContext
from app.models.events import MoAEvent
from app.outbound.adapter import ResponseAdapter, OutboundResponse

logger = logging.getLogger("moa.engine")


@dataclass
class SessionState:
    session_id: str
    context: StateContext
    state_stack: list[str]

    def __post_init__(self) -> None:
        if self.state_stack is None:
            self.state_stack = []


@dataclass
class HitlRequest:
    """Payload stored while waiting for HITL approval."""
    session_id: str
    trace_id: str
    agent_output: str
    intent: str
    agent_name: str
    channel: str
    target: str


class Engine:
    def __init__(
        self,
        router: Any = None,
        adapter: ResponseAdapter | None = None,
    ) -> None:
        self.router = router
        self.adapter = adapter or ResponseAdapter()
        self._pending_hitl: dict[str, HitlRequest] = {}

    async def handle_event(self, event: MoAEvent) -> SessionState:
        ctx = StateContext(state=State.INIT, session_id=event.session_id, trace_id=event.trace_id, metadata=event.context)
        current = ctx.state
        logger.info("incoming=%s trace=%s text=%r", event.event.value, event.trace_id, event.text)
        state_stack = list(event.context.get("state_stack", []))

        try:
            current = next_state(current, event.event)
        except Exception as exc:
            logger.exception("invalid transition: %s", exc)
            raise

        ctx.state = current
        if event.event == Event.SENSITIVE_DETECTED:
            state_stack = list(state_stack) + [current.value]
        elif event.event == Event.NEEDS_HUMAN:
            state_stack = list(state_stack) + [current.value]
            ctx.metadata["hitl_pending"] = True
        elif event.event == Event.HUMAN_APPROVED:
            ctx.metadata["hitl_pending"] = False
            if state_stack:
                state_stack = state_stack[:-1]
        elif event.event == Event.HUMAN_REJECTED:
            ctx.metadata["hitl_pending"] = False
            if state_stack:
                state_stack = state_stack[:-1]

        ctx.metadata["state_stack"] = state_stack
        return SessionState(session_id=event.session_id, context=ctx, state_stack=state_stack)

    async def respond(self, session_state: SessionState, text: str, *, channel: str, target: str) -> OutboundResponse:
        return self.adapter.adapt(text, channel=channel, target=target)

    def store_hitl(self, session_id: str, request: HitlRequest) -> None:
        self._pending_hitl[session_id] = request
        logger.info("hitl stored session=%s intent=%s", session_id, request.intent)

    def get_hitl(self, session_id: str) -> HitlRequest | None:
        return self._pending_hitl.get(session_id)

    def remove_hitl(self, session_id: str) -> None:
        self._pending_hitl.pop(session_id, None)
        logger.info("hitl resolved session=%s", session_id)
