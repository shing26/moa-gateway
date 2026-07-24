from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from app.agents.contract import AgentEnvelope, get_agent
import app.agents.loader
from app.channels.feishu import FeishuChannelAdapter, FeishuConfig
from app.channels.feishu_cards import ApprovalCard, FeishuCardSender, parse_card_callback
from app.config import settings
from app.engine import Engine, HitlRequest
from app.evaluator.evaluator import RuleEvaluator
from app.feature_flags import DEFAULT_FLAGS, FeatureFlagClient
from app.fsm.state_machine import Event as FsmEvent
from app.guard.guard_service import GuardianAction, GuardService, guard_service
from app.guard.permission_guard import FailClosedPermissionGuard
from app.middleware.flags import FeatureFlagMiddleware
from app.models.events import MoAEvent, PlatformEvent, new_trace_id
from app.observability.tracing import setup_tracing
from app.outbound.adapter import ResponseAdapter, OutboundResponse
from app.prompt_registry import PromptEntry, PromptRegistry
from app.prompt_registry.canary import CanaryConfig, select_canary_version
from app.router.intent_router import IntentRouter
from app.vectordb import VectorDBClient
from app.vectordb.retriever import ContextRetriever

logger = logging.getLogger("moa.gateway")
tracer: trace.Tracer

# ---- Shared infrastructure ----
_feishu_config: FeishuConfig | None = None
_card_sender: FeishuCardSender | None = None
_flag_client = FeatureFlagClient()
_prompt_registry = PromptRegistry()
_retriever = ContextRetriever(VectorDBClient())


def _init_feishu() -> None:
    global _feishu_config, _card_sender
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if app_id and app_secret:
        _feishu_config = FeishuConfig(app_id=app_id, app_secret=app_secret)
        _card_sender = FeishuCardSender(_feishu_config)
        logger.info("feishu card sender initialized")
    else:
        logger.warning("FEISHU_APP_ID / FEISHU_APP_SECRET not set; HITL cards disabled")


def _init_prompts() -> None:
    _prompt_registry.register(PromptEntry(
        agent_name="coder", version="default",
        system_prompt="You are a professional coding assistant.",
        metadata={"author": "system"},
    ))
    _prompt_registry.register(PromptEntry(
        agent_name="general", version="default",
        system_prompt="You are a general-purpose assistant.",
        metadata={"author": "system"},
    ))
    _prompt_registry.set_active("coder", "default")
    _prompt_registry.set_active("general", "default")
    _flag_client.seed(DEFAULT_FLAGS)
    logger.info("prompt registry initialized with defaults")



app = FastAPI(title="MoA Engine Gateway", version="0.1.0")
app.add_middleware(FeatureFlagMiddleware, client=_flag_client)

@app.exception_handler(Exception)
async def _debug_exception_handler(request: Request, exc: Exception):
    import traceback
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("unhandled exception: %s", "".join(tb))
    return JSONResponse(status_code=500, content={"error": type(exc).__name__, "detail": str(exc)[:500]})

router = IntentRouter()
adapter = ResponseAdapter()
evaluator = RuleEvaluator()
permission_guard = FailClosedPermissionGuard()
engine = Engine(router=router, adapter=adapter)
tracer = trace.get_tracer("moa-gateway")

@app.on_event("startup")
async def _startup() -> None:
    global tracer
    try:
        setup_tracing()
        logger.info("opentelemetry tracing enabled")
    except Exception:
        logger.warning("opentelemetry tracing unavailable; using no-op tracer")
    tracer = trace.get_tracer("moa-gateway")
    _init_feishu()
    _init_prompts()




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

        # Route intent and select agent.
        intent, fallback = await router.route(event.text)
        agent = get_agent(intent) or get_agent("general")
        agent_name = intent if agent else "general"
        for name in ("coder", "general"):
            if get_agent(name) is agent:
                agent_name = name
                break
        root_span.set_attribute("moa.intent", intent)
        root_span.set_attribute("moa.fallback", fallback)

        # Retrieve relevant context from vector store.
        retrieval = await _retriever.retrieve(event.text, session_id=event.session_id)

        # Select prompt version via canary.
        canary_enabled = await _flag_client.get("canary.enabled", False)
        canary_pct = await _flag_client.get("canary.traffic_pct", 10)
        canary_config = CanaryConfig(
            enabled=bool(canary_enabled),
            traffic_pct=int(canary_pct),
        )
        selected_prompt, selected_version = select_canary_version(
            event.session_id, _prompt_registry, agent_name, canary_config,
        )
        root_span.set_attribute("moa.prompt_version", selected_version)

        envelope = AgentEnvelope(
            trace_id=trace_id,
            session_id=event.session_id,
            user_raw_input=event.text,
            global_summary=retrieval.context,
            agent_local_slot={
                "intent": intent,
                "resource": intent,
                "prompt_version": selected_version,
                "system_prompt": selected_prompt.system_prompt if selected_prompt else "",
            },
        )

        with tracer.start_as_current_span("moa.agent.execute") as agent_span:
            agent_span.set_attribute("moa.agent", agent_name)
            raw_output = await agent.execute(envelope)

        with tracer.start_as_current_span("moa.evaluator.score") as eval_span:
            eval_result = await evaluator.score(raw_output, intent)
            eval_span.set_attribute("moa.eval.score", eval_result.score)
            eval_span.set_attribute("moa.eval.need_review", eval_result.need_human_review)

        # Guard evaluation (three-level: ALLOW / REVIEW / DENY).
        payload = {"intent": intent, "resource": intent, "role": os.environ.get("MOA_DEFAULT_ROLE", "operator")}
        verdict = guard_service.evaluate(agent_name, intent, payload, hitl_enabled=settings.hitl_enabled)
        root_span.set_attribute("moa.guard.action", verdict.action.value)
        root_span.set_attribute("moa.guard.reason", verdict.reason)

        if verdict.action == GuardianAction.REVIEW:
            hitl_request = HitlRequest(
                session_id=event.session_id,
                trace_id=trace_id,
                agent_output=raw_output,
                intent=intent,
                agent_name=agent_name,
                channel=channel,
                target=platform_event.session_id,
            )
            engine.store_hitl(event.session_id, hitl_request)

            if _card_sender:
                card = ApprovalCard(
                    session_id=event.session_id,
                    trace_id=trace_id,
                    agent_name=agent_name,
                    intent=intent,
                    agent_output=raw_output,
                    channel=channel,
                    target=platform_event.session_id,
                )
                await _card_sender.send_card(card)

            return JSONResponse({
                "trace_id": trace_id,
                "state": "SUSPENDED",
                "intent": intent,
                "status": "pending_review",
                "message": "Output requires human approval before delivery",
            })

        if verdict.action == GuardianAction.DENY:
            root_span.set_attribute("moa.guard.blocked", True)
            return JSONResponse({
                "trace_id": trace_id,
                "state": session_state.context.state.value,
                "intent": intent,
                "status": "blocked",
                "message": verdict.reason,
            })

        # ALLOW: proceed.
        with tracer.start_as_current_span("moa.guard.legacy_check") as legacy_span:
            legacy = await permission_guard.check(agent_name, {})
            legacy_span.set_attribute("moa.guard.legacy_allowed", legacy.allowed)

        with tracer.start_as_current_span("moa.adapter.adapt") as adapt_span:
            response = adapter.adapt(raw_output, channel=channel, target=platform_event.session_id)

        return JSONResponse({
            "trace_id": trace_id,
            "state": session_state.context.state.value,
            "intent": intent,
            "text": response.text,
            "need_human_review": eval_result.need_human_review or verdict.action != GuardianAction.ALLOW,
        })


@app.post("/webhook/callback")
async def webhook_callback(request: Request) -> JSONResponse:
    body = await request.json()
    parsed = parse_card_callback(body)
    if parsed is None:
        logger.warning("unparseable card callback: %s", body)
        return JSONResponse({"error": "invalid_callback_payload"}, status_code=400)

    session_id, trace_id, action = parsed
    logger.info("card callback session=%s action=%s", session_id, action)

    hitl = engine.get_hitl(session_id)
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
        trace_id=trace_id,
        event=fsm_event,
        session_id=session_id,
        text="",
        context={"source": "feishu_card_callback", "action": action},
    )
    session_state = await engine.handle_event(moa_event)

    if action == "approve":
        engine.remove_hitl(session_id)
        response = adapter.adapt(hitl.agent_output, channel=hitl.channel, target=hitl.target)
        return JSONResponse({
            "trace_id": trace_id,
            "state": session_state.context.state.value,
            "text": response.text,
            "status": "approved",
        })
    else:
        engine.remove_hitl(session_id)
        return JSONResponse({
            "trace_id": trace_id,
            "state": session_state.context.state.value,
            "status": "rejected",
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
