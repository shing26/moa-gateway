from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.channels.base import ChannelMessage
from app.channels.feishu import FeishuChannelAdapter, FeishuConfig
from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider
from app.channels.feishu_event import parse_feishu_event
from app.channels.feishu_signature import verify_verification_token
from app.config import settings
from app.deps import adapter, engine, pipeline
from app.middleware.auth import insecure_mode_enabled
from app.middleware.request_logger import bind_trace, log_request
from app.models.errors import ErrorCode
from app.fsm.state_machine import Event as FsmEvent
from app.models.events import MoAEvent, new_trace_id
from app.pipeline import PipelineResult


router = APIRouter()
_adapter = None
_seen_events = set()
_MAX_SEEN = 100
# fire-and-forget 的 HITL 闭环任务必须持引用：asyncio 只保留弱引用，
# 请求作用域结束后任务可能被 GC 回收、中途静默消失（官方文档明确警告）。
_background_tasks: set[asyncio.Task] = set()

logger = logging.getLogger("moa.routes.feishu")

async def get_adapter():
    global _adapter
    if _adapter is not None:
        return _adapter
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if app_id and app_secret:
        auth = FeishuTokenProvider(FeishuAuthConfig(app_id=app_id, app_secret=app_secret))
        _adapter = FeishuChannelAdapter(FeishuConfig(app_id=app_id, app_secret=app_secret), auth=auth)
    return _adapter

def _dedup(event_id: str) -> bool:
    global _seen_events
    if event_id in _seen_events:
        return False
    _seen_events.add(event_id)
    if len(_seen_events) > _MAX_SEEN:
        _seen_events.clear()
    return True


async def _safe_send(target: str, text: str, trace_id: str) -> None:
    adp = await get_adapter()
    if not adp:
        logger.error("card_action adapter is None; drop reply to %s", target)
        return
    await adp.send(ChannelMessage(channel="feishu", target=target, text=text, trace_id=trace_id))


async def _process_hitl_card(
    action: str, trace_id: str, session_id: str, operator_id: str = ""
) -> None:
    """卡片按钮点击后的真实 HITL 闭环，与 /webhook/callback 同一套语义：
    认领挂起请求 → FSM 推进 HUMAN_APPROVED / HUMAN_REJECTED → 送达或丢弃
    输出 → 审计留痕（guard_action=hitl_approve / hitl_reject / hitl_expired）。

    **认领用 `pop_hitl`（原子取走并删除）而非"读 → 判断 → 删"**：那张卡片被连点两次、
    或两个回调并发到达时，第二个调用者必须拿不到 payload，否则会重复送达 + 写两条审计。
    """
    hitl_id = trace_id or session_id
    started = time.time()
    # 后台任务在独立上下文中运行：显式绑定，保证决策审计与触发审批的
    # 请求共享同一 trace（人工决策因此可按 trace 回流评测）
    bind_trace(trace_id or session_id)
    duration_ms = 0.0
    outcome = "hitl_not_found"
    try:
        # 审批人白名单（`HITL_APPROVER_IDS`，非空即强制）。**必须在认领之前判**：
        # 不能让没有权限的人把待审批记录点掉——那样别人就再也批不了了。
        # 取不到点击者（平台没带 operator）而白名单非空时同样拒绝：验不了就不放行。
        if settings.hitl_approver_ids and operator_id not in settings.hitl_approver_ids:
            logger.warning(
                "card_action rejected: operator=%r not allowed (hitl_id=%s, configured=%d)",
                operator_id or "<unknown>", hitl_id, len(settings.hitl_approver_ids),
            )
            await _safe_send(session_id, "你没有审批该请求的权限", trace_id)
            outcome = "hitl_forbidden"
            return
        hitl = engine.session_store.pop_hitl(hitl_id)
        if hitl is None:
            # 不存在 / 已失效 / 已被（并发的另一次点击）认领，三种情况同一处理：
            # 不送达、不写决策审计。
            logger.warning("card_action hitl not available hitl_id=%s", hitl_id)
            await _safe_send(session_id, "该审批请求已失效或已被处理", trace_id)
            return
        ctx, expired = await engine.decide_hitl(
            session_id=session_id, trace_id=trace_id, approve=(action == "approve"),
        )
        # 审计字段 `hitl_duration_ms` 的语义必须与 /webhook/callback 一致：那里算的是
        # "从挂起到人工决定"的**真实时延**，而这里此前塞的是**回调处理耗时**（毫秒级）
        # —— 同名两义，且 dashboard 的"人工审批耗时分布"读的正是该字段（2026-09-23 修）。
        # `duration_ms` 仍保留给日志/outcome，表示本次处理耗时。
        human_latency_ms = (
            round((time.time() - hitl.created_at) * 1000, 1) if hitl.created_at > 0 else 0.0
        )
        if expired:
            # 重启后挂起记录还在、FSM 会话状态已丢：告知并结束（记录已被 pop 消耗）。
            duration_ms = round((time.time() - started) * 1000, 1)
            await log_request(
                None, 200, duration_ms, session_id=session_id,
                agent_name=hitl.agent_name, intent=hitl.intent,
                guard_action="hitl_expired", input_text="",
                output_text=hitl.agent_output[:2000], hitl_decision=action,
                hitl_duration_ms=human_latency_ms, hitl_kind=hitl.hitl_kind,
                hitl_operator=operator_id,
            )
            await _safe_send(
                session_id,
                "该审批已失效（审批可能已处理，或服务重启过），请重新发起该请求",
                trace_id,
            )
            outcome = "hitl_expired"
            return
        # 本地格式化在送达之前（纯本地操作，不产生副作用）；而挂起记录已在上面的
        # pop 里认领 —— 若此处格式化或随后发送失败，该记录不会回来，用户需重新发起。
        # 这是"单次决策"的代价：与之相比重复送达是更坏的语义。
        if action == "approve":
            adapted = adapter.adapt(hitl.agent_output, channel=hitl.channel, target=hitl.target)
            body = "✅ 已批准，以下是输出：\n" + (adapted.text or hitl.agent_output)
        else:
            body = "❌ 已拒绝，输出已按你的选择丢弃"
        duration_ms = round((time.time() - started) * 1000, 1)
        await log_request(
            None, 200, duration_ms, session_id=session_id,
            agent_name=hitl.agent_name, intent=hitl.intent,
            guard_action=f"hitl_{action}", input_text="",
            output_text=hitl.agent_output[:2000], hitl_decision=action,
            hitl_duration_ms=human_latency_ms, hitl_kind=hitl.hitl_kind,
            hitl_operator=operator_id,
        )
        await _safe_send(session_id, body, trace_id)
        outcome = f"hitl_{action}"
    except Exception:
        outcome = "hitl_error"
        logger.exception("card_action hitl processing failed trace=%s action=%s", trace_id, action)
        try:
            await _safe_send(session_id, "处理审批时出错了，请稍后重试", trace_id)
        except Exception:
            logger.exception("card_action error notify also failed trace=%s", trace_id)
    finally:
        logger.info(
            "card_action closed trace=%s action=%s outcome=%s duration_ms=%s",
            trace_id, action, outcome, duration_ms,
        )

@router.post("/feishu/event")
async def feishu_event(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error":"invalid_json"}, status_code=400)
    if isinstance(body, dict) and body.get("encrypt"):
        # 加密模式未实现（见 README 已知边界）。此前这种请求落到 "ignored" 分支被**静默丢弃**
        # ——配了加密后收不到任何事件、日志里也看不出原因。现在明确报错（2026-09-23）。
        logger.error(
            "feishu_event 收到加密事件（body.encrypt）但加密模式未实现："
            "请在开放平台关闭加密，或先实现 AES 解密 + X-Lark-Signature 校验"
        )
        return JSONResponse({"error": "encrypted_events_unsupported"}, status_code=400)
    if not verify_verification_token(
        body,
        settings.feishu_verification_token,
        allow_insecure=insecure_mode_enabled(settings.gateway_allow_insecure),
    ):
        # 此前本函数只看顶层 token，而 v2 的 token 在 header.token —— 于是"配了也
        # 从不比对"，等于这道门一直开着（2026-09-23 修）。拒绝时打点便于现网定位：
        # 若真实回调被拒，说明平台带的字段位置与我实现的不一致。
        logger.warning(
            "feishu_event rejected by token check: has_top_token=%s has_header_token=%s configured=%s",
            isinstance(body.get("token"), str) and bool(body.get("token")),
            isinstance(body.get("header"), dict) and bool(body["header"].get("token")),
            bool(settings.feishu_verification_token),
        )
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    header = body.get("header", {}) if isinstance(body.get("header"), dict) else {}
    event_id = header.get("event_id", body.get("event_id", ""))
    if event_id and not _dedup(event_id):
        return JSONResponse({"msg":"duplicate"})

    parsed = parse_feishu_event(body)
    logger.info("feishu_event_parsed event_type=%s body=%s", parsed["event_type"], json.dumps(body, ensure_ascii=False))
    print(f"[PARSE] event_type={parsed['event_type']!r} body={json.dumps(body, ensure_ascii=False)[:1000]}")

    if parsed["event_type"] == "url_verification":
        return JSONResponse({"challenge": parsed["challenge"]})
    if parsed["event_type"] == "card_action":
        action_value = parsed.get("action", {})
        action = action_value.get("action", "")
        trace_id = action_value.get("trace_id", "")
        session_id = parsed.get("chat_id", "")
        logger.info("card_action action=%s trace_id=%s chat_id=%s", action, trace_id, session_id)
        print(f"[CARD_ACTION] action={action!r} trace_id={trace_id!r} chat={session_id!r}")

        if action in ("approve", "reject"):
            # 飞书卡片回调有 3 秒响应窗口：真正的 HITL 闭环（FSM 推进、输出
            # 送达、审计留痕）必须异步执行、响应先行——在响应前 await 飞书
            # API 发消息会撑爆窗口，客户端报 200341"出错了，请稍后重试"。
            task = asyncio.create_task(
                _process_hitl_card(action, trace_id, session_id, parsed.get("operator_id") or "")
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        # 卡片回调响应契约：HTTP 200 + 空对象即"已受理"；返回业务结构会被
        # 客户端当作卡片更新解析失败，同样触发 200341。
        return JSONResponse({})
    if parsed["event_type"] not in ("event_callback", "im.message.receive_v1"):
        return JSONResponse({"msg":"ignored"})
    if not parsed["text"] or not parsed["chat_id"]:
        return JSONResponse({"msg":"no_content"})

    sid = parsed["chat_id"]
    moa_event = MoAEvent(
        trace_id=new_trace_id(), event=FsmEvent.MESSAGE_RECEIVED,
        session_id=sid, text=parsed["text"],
        context={"source": "feishu_event"},
        user_id=parsed.get("sender_id", ""),
    )
    try:
        result = await pipeline.run(moa_event, channel="feishu", target=sid, request=request)
    except Exception:
        import traceback
        traceback.print_exc()
        logger.exception("pipeline.run failed for trace_id=%s", moa_event.trace_id)
        result = PipelineResult(
            trace_id=moa_event.trace_id, state="", intent="", text="抱歉，处理消息时出错了",
            status="error", error_code=ErrorCode.INTERNAL_ERROR.value,
        )
    logger.info(
        "pipeline.run result trace_id=%s status=%s code=%s intent=%s text=%s",
        result.trace_id, result.status, result.error_code or "-", result.intent, result.text,
    )

    if result.status == "pending_review":
        reply = "输出需要人工审批"
    elif result.status == "suspended":
        reply = "已检测到敏感内容，消息已挂起"
    elif result.status == "reset":
        reply = result.text
    elif result.status == "error":
        reply = "抱歉，处理消息时出错了"
    else:
        reply = result.text

    adp = await get_adapter()
    if adp:
        await adp.send(ChannelMessage(channel="feishu", target=sid, text=reply, trace_id=result.trace_id))
    return JSONResponse({"msg":"ok"})
