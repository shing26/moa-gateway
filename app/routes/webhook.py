from __future__ import annotations
import time
from typing import Any
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.channels.feishu_cards import parse_card_callback
from app.deps import adapter, engine, logger, pipeline, tracer
from app.fsm.state_machine import Event as FsmEvent
from app.limit_providers.rate_limiter import rate_limiter
from app.middleware.request_logger import bind_trace, log_request
from app.models.errors import ErrorCode
from app.models.events import MoAEvent, PlatformEvent, new_trace_id

webhook_router = APIRouter()

@webhook_router.post("/webhook/callback")
async def webhook_callback(request: Request) -> JSONResponse:
    body = await request.json()
    parsed = parse_card_callback(body)
    if parsed is None:
        logger.warning("unparseable card callback: %s", body)
        return JSONResponse({"error": ErrorCode.INVALID_CALLBACK_PAYLOAD.value}, status_code=400)
    session_id, trace_id, action = parsed
    logger.info("card callback session=%s trace=%s action=%s", session_id, trace_id, action)
    # 卡片回调的审计与最初触发审批的请求共享同一 trace，决策可回流可复盘
    bind_trace(trace_id or session_id)
    hitl_id = trace_id or session_id
    # 原子认领（取走即删除）：并发第二次点击拿不到 payload → 404，不再重复送达 + 双审计
    hitl = engine.session_store.pop_hitl(hitl_id)
    if hitl is None:
        logger.warning("hitl request not available hitl_id=%s session=%s", hitl_id, session_id)
        return JSONResponse({"error": ErrorCode.HITL_REQUEST_NOT_FOUND.value}, status_code=404)
    if action not in ("approve", "reject"):
        return JSONResponse({"error": f"unknown_action:{action}"}, status_code=400)
    session_context, expired = await engine.decide_hitl(
        session_id=session_id, trace_id=trace_id, approve=(action == "approve"),
    )
    hitl_duration_ms = (
        round((time.time() - hitl.created_at) * 1000, 1) if hitl.created_at > 0 else 0.0
    )
    if expired:
        # 与 /feishu/event 同一语义：重启后会话状态已丢，告知并结束（记录已认领）。
        # 此前这里是裸 await，非法迁移会直接 500。
        await log_request(
            request, 200, 0, session_id=session_id, agent_name=hitl.agent_name,
            intent=hitl.intent, guard_action="hitl_expired", input_text="",
            output_text=hitl.agent_output[:2000], hitl_decision=action,
            hitl_duration_ms=hitl_duration_ms, hitl_kind=hitl.hitl_kind,
        )
        return JSONResponse({
            "trace_id": trace_id, "status": "expired",
            "message": "该审批已失效（审批可能已处理，或服务重启过），请重新发起",
        })
    if action == "approve":
        response = adapter.adapt(hitl.agent_output, channel=hitl.channel, target=hitl.target)
        await log_request(
            request, 200, 0, session_id=session_id, agent_name=hitl.agent_name,
            intent=hitl.intent, guard_action=f"hitl_{action}", input_text="",
            output_text=hitl.agent_output[:2000], hitl_decision=action,
            hitl_duration_ms=hitl_duration_ms, hitl_kind=hitl.hitl_kind,
        )
        return JSONResponse({
            "trace_id": trace_id, "state": session_context.state.value, "text": response.text, "status": "approved",
        })
    else:
        await log_request(
            request, 200, 0, session_id=session_id, agent_name=hitl.agent_name,
            intent=hitl.intent, guard_action=f"hitl_{action}", input_text="",
            output_text=hitl.agent_output[:2000], hitl_decision=action,
            hitl_duration_ms=hitl_duration_ms, hitl_kind=hitl.hitl_kind,
        )
        return JSONResponse({
            "trace_id": trace_id, "state": session_context.state.value, "status": "rejected",
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
            return JSONResponse({"error": ErrorCode.RATE_LIMITED.value, "message": "Too many requests. Try again later."}, status_code=429)
        trace_id = new_trace_id()
        root_span.set_attribute("moa.channel", channel)
        root_span.set_attribute("moa.trace_id", trace_id)

        event = MoAEvent(
            trace_id=trace_id,
            event=_map_event(platform_event),
            session_id=platform_event.session_id,
            text=platform_event.payload.get("text", ""),
            context={"source": "webhook", "channel": channel},
            user_id=platform_event.user_id,
        )

        result = await pipeline.run(
            event, channel=channel, target=platform_event.session_id, request=request,
        )

        if result.status == "command":
            return JSONResponse({
                "text": result.text, "state": result.state, "intent": result.intent,
                "status": "command",
            })
        if result.status == "reset":
            return JSONResponse({
                "text": result.text, "state": result.state, "intent": result.intent,
                "status": "reset",
            })
        if result.status == "suspended":
            return JSONResponse({
                "trace_id": result.trace_id, "state": result.state,
                "intent": result.intent, "status": "suspended", "message": result.text,
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
                "error": result.error_code or ErrorCode.AGENT_FAILED.value,
                "message": result.text, "status": "error",
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
