from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from app.fsm.state_machine import Event, State, next_state, StateContext
from app.models.events import MoAEvent

logger = logging.getLogger("moa.engine")


@dataclass
class SessionState:
    session_id: str
    context: StateContext
    state_stack: list[str] = None

    def __post_init__(self) -> None:
        if self.state_stack is None:
            self.state_stack = []


class Engine:
    def __init__(self, router: Any | None = None) -> None:
        self.router = router

    async def handle_event(self, event: MoAEvent) -> SessionState:
        ctx = StateContext(state=State.INIT, session_id=event.session_id, trace_id=event.trace_id)
        current_state = ctx.state
        logger.info("incoming=%s trace=%s text=%r", event.event, event.trace_id, event.text)

        if event.event == Event.MESSAGE_RECEIVED:
            state_stack = []
        else:
            state_stack = event.context.get("state_stack", [])

        try:
            current_state = next_state(current_state, event.event)
        except Exception as exc:
            logger.exception("invalid transition: %s", exc)
            raise

        ctx.state = current_state
        if event.event == Event.SENSITIVE_DETECTED:
            state_stack = list(state_stack) + [current_state.name]
        elif event.event == Event.HUMAN_APPROVED:
            state_stack = state_stack[:-1] if state_stack else state_stack

        return SessionState(session_id=event.session_id, context=ctx, state_stack=state_stack)
