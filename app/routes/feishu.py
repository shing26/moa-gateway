from __future__ import annotations
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
from app.fsm.state_machine import Event as FsmEvent
from app.models.events import MoAEvent, new_trace_id
from app.pipeline import PipelineResult

router = APIRouter()
_adapter = None
_seen_events = set()
_MAX_SEEN = 100

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

    if parsed["event_type"] == "url_verification":
        return JSONResponse({"challenge": parsed["challenge"]})
    if parsed["event_type"] == "card_action":
        return JSONResponse({"msg":"ok"})
    if parsed["event_type"] not in ("event_callback", "im.message.receive_v1"):
        return JSONResponse({"msg":"ignored"})
    if not parsed["text"] or not parsed["chat_id"]:
        return JSONResponse({"msg":"no_content"})

    sid = parsed["chat_id"]
    moa_event = MoAEvent(
        trace_id=new_trace_id(), event=FsmEvent.MESSAGE_RECEIVED,
        session_id=sid, text=parsed["text"],
        context={"source": "feishu_event"},
    )
    try:
        result = await pipeline.run(moa_event, channel="feishu", target=sid, request=request)
    except Exception:
        result = PipelineResult(trace_id=moa_event.trace_id, state="", intent="", text="抱歉，处理消息时出错了", status="error")

    if result.status == "pending_review":
        reply = "输出需要人工审批"
    elif result.status == "error":
        reply = "抱歉，处理消息时出错了"
    else:
        reply = result.text

    adp = await get_adapter()
    if adp:
        await adp.send(ChannelMessage(channel="feishu", target=sid, text=reply, trace_id=result.trace_id))
    return JSONResponse({"msg":"ok"})
