from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from app.agents.contract import AgentEnvelope, get_agent
from app.config import settings
from app.engine import Engine
from app.evaluator.evaluator import RuleEvaluator
from app.guard.permission_guard import FailClosedPermissionGuard
from app.models.events import MoAEvent, PlatformEvent, new_trace_id
from app.observability.tracing import setup_tracing
from app.outbound.adapter import ResponseAdapter, OutboundResponse
from app.router.intent_router import IntentRouter

tracer: trace.Tracer


@app.on_event("startup")
async def _init_tracing() -> None:
    global tracer
    try:
        setup_tracing()
        logger.info("opentelemetry tracing enabled")
    except Exception:
        logger.warning("opentelemetry tracing unavailable; using no-op tracer")
    tracer = trace.get_tracer("moa-gateway")

logger = logging.getLogger("moa.gateway")
app = FastAPI(title="MoA Engine Gateway", version="0.1.0")

router = IntentRouter()
adapter = ResponseAdapter()
evaluator = RuleEvaluator()
guard = FailClosedPermissionGuard()
engine = Engine(router=router, adapter=adapter)
tracer = trace.get_tracer("moa-gateway")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/webhook/{channel}")
async def webhook(channel: str, request: Request) -> JSONResponse:
    with tracer.start_as_current_span("moa.webhook.receive") as root_span:
        body = await request.json()
        platform_event = _decode_platform(channel, body)
        trace_id = new_trace_id()
        root_span.set_attribute("moa.channel", channel)
        root_span.set_attribute("moa.trace_id", trace_id)

        event = MoAEvent(
            trace_id=trace_id,
            event=_map_event(platform_event),
            session_id=platform_event.session_id,
            text=platform_event.payload.get("text", ""),
            context={"source": "webhook", "channel": channel},
        )

        with tracer.start_as_current_span("moa.engine.handle_event") as fsm_span:
            session_state = await engine.handle_event(event)
            fsm_span.set_attribute("moa.state", session_state.context.state.value)

        # Route intent and execute the appropriate agent.
        intent, fallback = await router.route(event.text)
        agent = get_agent(intent) or get_agent("general")
        agent_name = intent if agent else "general"
        root_span.set_attribute("moa.intent", intent)
        root_span.set_attribute("moa.fallback", fallback)

        envelope = AgentEnvelope(
            trace_id=trace_id,
            session_id=event.session_id,
            user_raw_input=event.text,
            global_summary="",
            agent_local_slot={},
        )

        with tracer.start_as_current_span("moa.agent.execute") as agent_span:
            agent_span.set_attribute("moa.agent", agent_name)
            raw_output = await agent.execute(envelope)

        with tracer.start_as_current_span("moa.evaluator.score") as eval_span:
            eval_result = await evaluator.score(raw_output, intent)
            eval_span.set_attribute("moa.eval.score", eval_result.score)
            eval_span.set_attribute("moa.eval.need_review", eval_result.need_human_review)

        with tracer.start_as_current_span("moa.guard.check") as guard_span:
            decision = await guard.check(agent_name, {})
            guard_span.set_attribute("moa.guard.allowed", decision.allowed)

        with tracer.start_as_current_span("moa.adapter.adapt") as adapt_span:
            response = adapter.adapt(raw_output, channel=channel, target=platform_event.session_id)

        return JSONResponse(
            {
                "trace_id": trace_id,
                "state": session_state.context.state.value,
                "intent": intent,
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
    if any(k in text for k in ("cancel", "\u53d6\u6d88", "reset", "\u91cd\u7f6e")):
        return Event.RESET
    if any(k in text for k in ("debug", "\u9519\u8bef", "\u62a5\u9519")):
        return Event.SENSITIVE_DETECTED
    return Event.MESSAGE_RECEIVED
