from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.engine import Engine
from app.evaluator.evaluator import RuleEvaluator
from app.guard.permission_guard import FailClosedPermissionGuard
from app.models.events import MoAEvent, PlatformEvent, new_trace_id
from app.outbound.adapter import ResponseAdapter, OutboundResponse
from app.router.intent_router import IntentRouter

logger = logging.getLogger("moa.gateway")
app = FastAPI(title="MoA Engine Gateway", version="0.1.0")

router = IntentRouter()
adapter = ResponseAdapter()
evaluator = RuleEvaluator()
guard = FailClosedPermissionGuard()
engine = Engine(router=router, adapter=adapter)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/webhook/{channel}")
async def webhook(channel: str, request: Request) -> JSONResponse:
    body = await request.json()
    platform_event = _decode_platform(channel, body)
    trace_id = new_trace_id()
    event = MoAEvent(
        trace_id=trace_id,
        event=_map_event(platform_event),
        session_id=platform_event.session_id,
        text=platform_event.payload.get("text", ""),
        context={"source": "webhook", "channel": channel},
    )
    session_state = await engine.handle_event(event)
    raw_output = f"[stub] {event.text}"
    eval_result = await evaluator.score(raw_output, "assistant")
    decision = await guard.check("assistant", {})
    response = adapter.adapt(raw_output, channel=channel, target=platform_event.session_id)
    return JSONResponse(
        {
            "trace_id": trace_id,
            "state": session_state.context.state.value,
            "text": response.text,
            "need_human_review": eval_result.need_human_review or not decision.allowed,
        }
    )


def _decode_platform(channel: str, body: dict[str, Any]) -> PlatformEvent:
    return PlatformEvent(
        platform=channel,
        message_id=str(body.get("message_id") or body.get("id", "")),
        session_id=str(body.get("session_id") or body.get("chat_id", "")),
        user_id=str(body.get("user_id") or body.get("sender", "")),
        payload=body,
    )


def _map_event(platform_event: PlatformEvent):
    from app.fsm.state_machine import Event
    text = (platform_event.payload.get("text") or "").lower()
    if any(k in text for k in ("cancel", "取消", "reset", "重置")):
        return Event.RESET
    if any(k in text for k in ("debug", "错误", "报错")):
        return Event.SENSITIVE_DETECTED
    return Event.MESSAGE_RECEIVED
