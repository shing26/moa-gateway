from __future__ import annotations
import logging, os, time
from typing import Any
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from app.agents.contract import AgentEnvelope, get_agent
import app.agents.loader
from app.channels.feishu_cards import ApprovalCard, parse_card_callback
from app.config import settings
from app.command_mode import MODES, parse_command
from app.deps import (
    command_mode,
    memory,
    _card_sender, _flag_client, _prompt_registry, _retriever,
    router, adapter, evaluator, engine,
    tracer, logger, init_feishu, init_prompts,
)
from app.engine import HitlRequest, SessionStore
from app.fsm.state_machine import Event as FsmEvent
from app.guard.guard_service import GuardianAction, guard_service
from app.limit_providers.rate_limiter import rate_limiter
from app.middleware.request_logger import log_request
from app.models.events import MoAEvent, PlatformEvent, new_trace_id
from app.outbound.adapter import OutboundResponse
from app.prompt_registry.canary import CanaryConfig, select_canary_version
from app.vectordb.retriever import ContextRetriever

webhook_router = APIRouter()

@webhook_router.post("/webhook/{channel}")
async def webhook(channel: str, request: Request) -> JSONResponse:
    start = time.monotonic()
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

        with tracer.start_as_current_span("moa.engine.handle_event") as fsm_span:
            session_state = await engine.handle_event(event)
            fsm_span.set_attribute("moa.state", session_state.context.state.value)

        # Handle /commands
        text = event.text.strip()
        if text.startswith("/"):
            parsed = parse_command(text)
            if parsed:
                cmd_key, label = parsed
                if cmd_key in ("help", ""):
                    help_text = "可用指令:\n/coding - 编程模式\n/translate - 翻译模式\n/search - 搜索模式\n/analyze - 分析模式\n/default - 默认模式"
                    await log_request(
                        request, 200, (time.monotonic() - start) * 1000,
                        event.session_id, "command", "help", "", event.text, help_text,
                    )
                    return JSONResponse({"text": help_text, "state": "ROUTED", "intent": "help"})
                cmd_info = MODES.get(cmd_key, {})
                command_mode.set(event.session_id, cmd_info.get("intent") or "")
                mode_label = cmd_info.get("label", cmd_key)
                await log_request(
                    request, 200, (time.monotonic() - start) * 1000,
                    event.session_id, "command", cmd_key, "", event.text,
                    "已切换至 " + mode_label + " 模式",
                )
                return JSONResponse({"text": "已切换至 " + mode_label + " 模式", "state": "ROUTED", "intent": cmd_key})
            return JSONResponse({"text": "未知指令，发送 /help 查看可用指令", "state": "ROUTED", "intent": "help"})

        intent, fallback = await router.route(event.text)
        # Check for forced mode from /commands
        forced = command_mode.get(event.session_id)
        if forced:
            intent = forced
        agent = get_agent(intent) or get_agent("general")
        agent_name = intent if agent else "general"
        for name in ("coder", "general"):
            if get_agent(name) is agent:
                agent_name = name
                break
        root_span.set_attribute("moa.intent", intent)
        root_span.set_attribute("moa.fallback", fallback)

        retrieval = await _retriever.retrieve(event.text, session_id=event.session_id)

        canary_enabled = await _flag_client.get("canary.enabled", False)
        canary_pct = await _flag_client.get("canary.traffic_pct", 10)
        canary_config = CanaryConfig(enabled=bool(canary_enabled), traffic_pct=int(canary_pct))
        selected_prompt, selected_version = select_canary_version(
            event.session_id, _prompt_registry, agent_name, canary_config,
        )
        root_span.set_attribute("moa.prompt_version", selected_version)

        conversation_history = memory.get_history(event.session_id)
        envelope = AgentEnvelope(
            trace_id=trace_id,
            session_id=event.session_id,
            user_raw_input=event.text,
            global_summary=retrieval.context,
            history=tuple(conversation_history),
            agent_local_slot={
                "intent": intent,
                "resource": intent,
                "prompt_version": selected_version,
                "system_prompt": selected_prompt.system_prompt if selected_prompt else "",
            },
        )

        with tracer.start_as_current_span("moa.agent.execute") as agent_span:
            agent_span.set_attribute("moa.agent", agent_name)
            try:
                raw_output = await agent.execute(envelope)
            except Exception:
                await log_request(
                    request, 500, (time.monotonic() - start) * 1000,
                    event.session_id, agent_name, intent, "error", event.text,
                    "agent execution failed",
                )
                raise

        with tracer.start_as_current_span("moa.evaluator.score") as eval_span:
            eval_result = await evaluator.score(raw_output, intent)
            eval_span.set_attribute("moa.eval.score", eval_result.score)
            eval_span.set_attribute("moa.eval.need_review", eval_result.need_human_review)

        payload = {"intent": intent, "resource": intent, "role": os.environ.get("MOA_DEFAULT_ROLE", "operator")}
        verdict = guard_service.evaluate(agent_name, intent, payload, hitl_enabled=settings.hitl_enabled)
        root_span.set_attribute("moa.guard.action", verdict.action.value)
        root_span.set_attribute("moa.guard.reason", verdict.reason)

        if verdict.action == GuardianAction.REVIEW:
            hitl_request = HitlRequest(
                session_id=event.session_id, trace_id=trace_id, agent_output=raw_output,
                intent=intent, agent_name=agent_name, channel=channel, target=platform_event.session_id,
            )
            engine.session_store.store_hitl(event.session_id, hitl_request)
            if _card_sender:
                card = ApprovalCard(
                    session_id=event.session_id, trace_id=trace_id, agent_name=agent_name,
                    intent=intent, agent_output=raw_output, channel=channel, target=platform_event.session_id,
                )
                await _card_sender.send_card(card)
            await log_request(
                request, 200, (time.monotonic() - start) * 1000,
                event.session_id, agent_name, intent, "review", event.text, raw_output,
            )
            return JSONResponse({
                "trace_id": trace_id, "state": "SUSPENDED", "intent": intent,
                "status": "pending_review", "message": "Output requires human approval before delivery",
            })

        if verdict.action == GuardianAction.DENY:
            root_span.set_attribute("moa.guard.blocked", True)
            await log_request(
                request, 200, (time.monotonic() - start) * 1000,
                event.session_id, agent_name, intent, "deny", event.text, verdict.reason,
            )
            return JSONResponse({
                "trace_id": trace_id, "state": session_state.context.state.value,
                "intent": intent, "status": "blocked", "message": verdict.reason,
            })


        with tracer.start_as_current_span("moa.adapter.adapt") as adapt_span:
            response = adapter.adapt(raw_output, channel=channel, target=platform_event.session_id)

        memory.add(event.session_id, event.text, response.text)
        await log_request(
            request, 200, (time.monotonic() - start) * 1000,
            event.session_id, agent_name, intent, verdict.action.value, event.text, response.text,
        )
        return JSONResponse({
            "trace_id": trace_id, "state": session_state.context.state.value,
            "intent": intent, "text": response.text,
            "need_human_review": eval_result.need_human_review or verdict.action != GuardianAction.ALLOW,
        })


@webhook_router.post("/webhook/callback")
async def webhook_callback(request: Request) -> JSONResponse:
    body = await request.json()
    parsed = parse_card_callback(body)
    if parsed is None:
        logger.warning("unparseable card callback: %s", body)
        return JSONResponse({"error": "invalid_callback_payload"}, status_code=400)
    session_id, trace_id, action = parsed
    logger.info("card callback session=%s action=%s", session_id, action)
    hitl = engine.session_store.get_hitl(session_id)
    if hitl is None:
        logger.warning("hitl request not found for session=%s", session_id)
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
        engine.session_store.remove_hitl(session_id)
        response = adapter.adapt(hitl.agent_output, channel=hitl.channel, target=hitl.target)
        return JSONResponse({
            "trace_id": trace_id, "state": session_state.context.state.value, "text": response.text, "status": "approved",
        })
    else:
        engine.session_store.remove_hitl(session_id)
        return JSONResponse({
            "trace_id": trace_id, "state": session_state.context.state.value, "status": "rejected",
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
