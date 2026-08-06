from __future__ import annotations
import time
from typing import Any
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.channels.feishu_cards import parse_card_callback
from app.deps import adapter, engine, logger, pipeline, tracer
from app.fsm.state_machine import Event as FsmEvent
from app.limit_providers.rate_limiter import rate_limiter
from app.middleware.request_logger import log_request
from app.models.events import MoAEvent, PlatformEvent, new_trace_id

webhook_router = APIRouter()

@webhook_router.post("/webhook/callback")
async def webhook_callback(request: Request) -> JSONResponse:
    body = await request.json()
    parsed = parse_card_callback(body)
    if parsed is None:
        logger.warning("unparseable card callback: %s", body)
        return JSONResponse({"error": "invalid_callback_payload"}, status_code=400)
    session_id, trace_id, action = parsed
    logger.info("card callback session=%s trace=%s action=%s", session_id, trace_id, action)
    hitl_id = trace_id or session_id
    hitl = engine.session_store.get_hitl(hitl_id)
    if hitl is None:
        logger.warning("hitl request not found hitl_id=%s session=%s", hitl_id, session_id)
        return JSONResponse({"error": "hitl_request_not_found"}, status_code=404)
    if action == "approve":
        fsm_event = FsmEvent.HUMAN_APPROVED
    elif action == "reject":
        fsm_event = FsmEvent.HUMAN_REJECTED
    else:
        return JSONResponse({"error": f"unknown_action:{action}"}, status_code=400)
    moa_event = MoAEvent(
        trace_id=trace_id, event=fsm_event, session_id=session_id, text="",
        context={"source": "feishu_card_callback", "action": action},
    )
    session_state = await engine.handle_event(moa_event)
    if action == "approve":
        engine.session_store.remove_hitl(hitl_id)
        response = adapter.adapt(hitl.agent_output, channel=hitl.channel, target=hitl.target)
        hitl_duration_ms = round((time.time() - hitl.created_at) * 1000, 1) if hitl.created_at > 0 else 0.0
        await log_request(
            request, 200, 0, session_id=session_id, agent_name=hitl.agent_name,
            intent=hitl.intent, guard_action=f"hitl_{action}", input_text="",
            output_text=hitl.agent_output[:2000], hitl_decision=action,
            hitl_duration_ms=hitl_duration_ms,
        )
        return JSONResponse({
            "trace_id": trace_id, "state": session_state.context.state.value, "text": response.text, "status": "approved",
        })
    else:
        engine.session_store.remove_hitl(hitl_id)
        hitl_duration_ms = round((time.time() - hitl.created_at) * 1000, 1) if hitl.created_at > 0 else 0.0
        await log_request(
            request, 200, 0, session_id=session_id, agent_name=hitl.agent_name,
            intent=hitl.intent, guard_action=f"hitl_{action}", input_text="",
            output_text=hitl.agent_output[:2000], hitl_decision=action,
            hitl_duration_ms=hitl_duration_ms,
        )
        return JSONResponse({
            "trace_id": trace_id, "state": session_state.context.state.value, "status": "rejected",
        })


@webhook_router.post("/webhook/{channel}")
async def webhook(channel: str, request: Request) -> JSONResponse:
    with tracer.start_as_current_span("moa.webhook.receive") as root_span:
        body = await request.json()
        platform_event = _decode_platform(channel, body)
        rate_key = platform_event.session_id or platform_event.user_id or "anonymous"
        allowed, remaining = await rate_limiter.check(rate_key)
        if not allowed:
            await log_request(request, 429, 0, rate_key, "", "", "denied")
            return JSONResponse({"error": "rate_limited", "message": "Too many requests. Try again later."}, status_code=429)
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

        result = await pipeline.run(
            event, channel=channel, target=platform_event.session_id, request=request,
        )

        if result.status == "command":
            return JSONResponse({
                "text": result.text, "state": result.state, "intent": result.intent,
                "status": "command",
            })
        if result.status == "pending_review":
            return JSONResponse({
                "trace_id": result.trace_id, "state": result.state, "intent": result.intent,
                "status": "pending_review", "message": result.text,
            })
        if result.status == "blocked":
            return JSONResponse({
                "trace_id": result.trace_id, "state": result.state,
                "intent": result.intent, "status": "blocked", "message": result.text,
            })
        if result.status == "error":
            return JSONResponse({
                "error": "agent_failed", "message": result.text, "status": "error",
            }, status_code=500)
        return JSONResponse({
            "trace_id": result.trace_id, "state": result.state,
            "intent": result.intent, "text": result.text,
            "need_human_review": result.need_human_review, "status": "ok",
        })


def _decode_platform(channel: str, body: dict[str, Any]) -> PlatformEvent:
    return PlatformEvent(
        platform=channel,
        message_id=str(body.get("message_id") or body.get("id", "")),
        session_id=str(body.get("session_id") or body.get("chat_id", "")),
        user_id=str(body.get("user_id") or body.get("sender", "")),
        payload=body,
    )

def _map_event(platform_event: PlatformEvent):
    text = (platform_event.payload.get("text") or "").lower()
    if any(k in text for k in ("cancel", chr(21462)+chr(28040), "reset", chr(37325)+chr(32622))):
        return FsmEvent.RESET
    if any(k in text for k in ("debug", chr(38169)+chr(35823), chr(25253)+chr(38169))):
        return FsmEvent.SENSITIVE_DETECTED
    return FsmEvent.MESSAGE_RECEIVED
