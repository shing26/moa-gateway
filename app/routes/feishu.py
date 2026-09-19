from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.channels.base import ChannelMessage
from app.channels.feishu import FeishuChannelAdapter, FeishuConfig
from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider
from app.channels.feishu_event import parse_feishu_event
from app.channels.feishu_signature import verify_verification_token
from app.config import settings
from app.deps import pipeline
from app.models.errors import ErrorCode
from app.fsm.state_machine import Event as FsmEvent
from app.models.events import MoAEvent, new_trace_id
from app.pipeline import PipelineResult


router = APIRouter()
_adapter = None
_seen_events = set()
_MAX_SEEN = 100

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

@router.post("/feishu/event")
async def feishu_event(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error":"invalid_json"}, status_code=400)
    if not verify_verification_token(body, settings.feishu_verification_token, settings.feishu_encrypt_key):
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
            adp = await get_adapter()
            logger.info("card_action adapter=%s send_target=%s", type(adp).__name__ if adp else None, session_id)
            if adp:
                try:
                    reply = f"审批结果：{'✅ 已批准' if action == 'approve' else '❌ 已拒绝'}\nTrace: {trace_id}"
                    ok = await adp.send(
                        ChannelMessage(
                            channel="feishu",
                            target=session_id,
                            text=reply,
                            trace_id=trace_id,
                        )
                    )
                    logger.info("card_action send_result=%s reply=%s", ok, reply)
                except Exception as exc:
                    logger.exception("card_action send_exception=%s", exc)
            else:
                logger.error("card_action adapter is None")
        return JSONResponse({"msg": "ok"})
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
