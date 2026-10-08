from __future__ import annotations
import time
from typing import Any
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.channels.feishu_cards import parse_card_callback
from app.config import settings
from app.deps import adapter, engine, logger, merge_store, pipeline, tracer
from app.fsm.state_machine import Event as FsmEvent
from app.limit_providers.rate_limiter import rate_limiter
from app.channels.feishu_signature import verify_verification_token
from app.middleware.auth import approver_gate_error, insecure_mode_enabled
from app.middleware.request_logger import bind_trace, log_request
from app.models.errors import ErrorCode
from app.models.events import MoAEvent, PlatformEvent, new_trace_id
from apps.code_review_pipeline.merge_executor import MergeExecutor
from apps.code_review_pipeline.routing.github_provider import build_github_client

webhook_router = APIRouter()

@webhook_router.post("/webhook/callback")
async def webhook_callback(request: Request) -> JSONResponse:
    body = await request.json()
    parsed = parse_card_callback(body)
    if parsed is None:
        logger.warning("unparseable card callback: %s", body)
        return JSONResponse({"error": ErrorCode.INVALID_CALLBACK_PAYLOAD.value}, status_code=400)
    session_id, trace_id, action = parsed
    # 与 /feishu/event 同一道门（探索性验收 D3，2026-09-29）。此前这条路径的 token 校验
    # 只有"带了 X-Lark-Token 就必须对"（auth.py 中间件），**没带直接放行** ——公网可达时，
    # 知道 session/trace 的人可以伪造审批回调。两条回调路径必须同一姿态：配了 token
    # 就必须对；没配只在显式 insecure 时放行（与审批人闸门同一三态）。
    if not verify_verification_token(
        body,
        settings.feishu_verification_token,
        allow_insecure=insecure_mode_enabled(settings.gateway_allow_insecure),
    ):
        logger.warning("card callback rejected by verification token check")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    logger.info("card callback session=%s trace=%s action=%s", session_id, trace_id, action)
    # 卡片回调的审计与最初触发审批的请求共享同一 trace，决策可回流可复盘
    bind_trace(trace_id or session_id)
    hitl_id = trace_id or session_id
    if not trace_id:
        logger.warning(
            "card callback 未携带 trace_id，本次决策无法按 trace 回流评测（hitl_id=%s）", hitl_id
        )
    # **校验全部在认领之前**：非法 action 与无权限的点击都不该消耗挂起记录
    # （消耗了就代表"这次审批没了"，别人再也批不了）。这条顺序是 2026-09-23 由
    # test_webhook_callback_refuses_operator_outside_allowlist 逼出来的——
    # 我最初把白名单校验插在 pop 之后，测试当场指出记录已被消耗。
    if action not in ("approve", "reject"):
        return JSONResponse({"error": f"unknown_action:{action}"}, status_code=400)
    # 点击者：v1 在顶层 open_id/user_id；v2 卡片回调在 event.operator（探索性验收 D3：
    # webhook 路径此前只读顶层，白名单模式下 v2 流量的**真**审批人会被 403）。
    _event = body.get("event") if isinstance(body.get("event"), dict) else {}
    _operator = _event.get("operator") if isinstance(_event.get("operator"), dict) else {}
    operator_id = str(
        body.get("open_id")
        or body.get("user_id")
        or _operator.get("open_id")
        or _operator.get("user_id")
        or ""
    )
    gate_error = approver_gate_error(
        operator_id,
        settings.hitl_approver_ids,
        raw_insecure=settings.gateway_allow_insecure,
    )
    if gate_error is not None:
        logger.warning("card callback rejected: %s (hitl_id=%s)", gate_error, hitl_id)
        return JSONResponse(
            {"error": ErrorCode.UNAUTHORIZED.value, "message": "没有审批该请求的权限"},
            status_code=403,
        )
    # 原子认领（取走即删除）：并发第二次点击拿不到 payload → 404，不再重复送达 + 双审计
    hitl = engine.session_store.pop_hitl(hitl_id)
    if hitl is None:
        logger.warning("hitl request not available hitl_id=%s session=%s", hitl_id, session_id)
        return JSONResponse({"error": ErrorCode.HITL_REQUEST_NOT_FOUND.value}, status_code=404)
    # ADR-021 合并审批分叉：merge_approval 不走聊天 FSM，直接执行合并
    if hitl.hitl_kind == "merge_approval":
        return await _handle_merge_approval(
            hitl_id=hitl_id,
            action=action,
            operator_id=operator_id,
            session_id=session_id,
            trace_id=trace_id,
        )
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
            hitl_operator=operator_id,
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
            hitl_operator=operator_id,
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
            hitl_operator=operator_id,
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

# 指令词表：**整条消息**必须就是其中之一才算指令（含 `/` 前缀写法）。
# 中文用 chr() 拼、与文件既有写法一致（避开编码问题）。
_RESET_COMMANDS = frozenset(
    {
        "/reset", "/cancel", "/clear", "reset", "cancel", "clear",
        chr(21462) + chr(28040),  # 取消
        chr(37325) + chr(32622),  # 重置
    }
)
_SENSITIVE_COMMANDS = frozenset(
    {
        "/debug", "debug",
        chr(35843) + chr(35797),  # 调试
        chr(25253) + chr(38169),  # 报错
    }
)


def _map_event(platform_event: PlatformEvent):
    """把平台消息映射成 FSM 事件。**只认"整条消息就是指令"**（全等，大小写不敏感）。

    此前是子串匹配（探索性验收 D2，2026-09-29）："怎么**取消**订阅" 会被当成重置指令
    清空会话，"看看这个**报错**" 会被判敏感挂起——而挂起没有审批出口，用户只能再发一次
    reset 才能继续。指令与对话必须分得开：一句正常的话里出现"取消"不该丢掉上下文。
    """
    text = (platform_event.payload.get("text") or "").strip().lower()
    if text in _RESET_COMMANDS:
        return FsmEvent.RESET
    if text in _SENSITIVE_COMMANDS:
        return FsmEvent.SENSITIVE_DETECTED
    return FsmEvent.MESSAGE_RECEIVED


async def _handle_merge_approval(
    *,
    hitl_id: str,
    action: str,
    operator_id: str,
    session_id: str,
    trace_id: str,
) -> JSONResponse:
    """ADR-021 合并审批回调：执行合并并写审计。

    **不走** ``engine.decide_hitl()``：合并审批是独立的写操作，与聊天 FSM 的
    会话状态无关。
    """
    from apps.code_review_pipeline.task_audit import record_human_decision

    executor = MergeExecutor(merge_store, build_github_client())
    try:
        outcome = await executor.execute(hitl_id, action, operator_id)
    except Exception as exc:
        logger.exception("merge approval execution failed: %s", exc)
        return JSONResponse(
            {"error": "merge_execution_failed", "message": str(exc)},
            status_code=500,
        )

    # 写审计：谁批的、批了什么
    try:
        await record_human_decision(
            task_id=hitl_id,
            repo=hitl_id.split("#")[0],
            operator=operator_id,
            decision=action,
        )
    except Exception:
        logger.exception("failed to record human decision for %s", hitl_id)

    if outcome.merged:
        return JSONResponse({
            "trace_id": trace_id,
            "status": "merged",
            "sha": outcome.sha,
        })
    return JSONResponse({
        "trace_id": trace_id,
        "status": outcome.status,
        "message": outcome.message,
    })
