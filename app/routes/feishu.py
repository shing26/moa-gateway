from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import json, logging, os, time
from app.channels.feishu_event import parse_feishu_event
from app.channels.feishu import FeishuChannelAdapter, FeishuConfig
from app.channels.feishu_auth import FeishuAuthConfig, FeishuTokenProvider
from app.channels.base import ChannelMessage
from app.agents.contract import AgentEnvelope, get_agent
from app.command_mode import MODES, parse_command
from app.deps import _retriever, memory, command_mode
from app.fsm.state_machine import Event as FsmEvent
from app.models.events import MoAEvent, new_trace_id
from app.engine import Engine
from app.middleware.request_logger import log_request
from app.router.intent_router import IntentRouter
from app.outbound.adapter import ResponseAdapter

logger = logging.getLogger("moa.routes.feishu")
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

_engine = Engine(router=IntentRouter(), adapter=ResponseAdapter())

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

    header = body.get("header", {}) if isinstance(body.get("header"), dict) else {}
    event_id = header.get("event_id", body.get("event_id", ""))
    if event_id and not _dedup(event_id):
        return JSONResponse({"msg":"duplicate"})

    parsed = parse_feishu_event(body)
    with open("feishu_debug.log", "a", encoding="utf-8") as f:
        f.write("EVENT: " + json.dumps(parsed, ensure_ascii=False)[:200] + "\n")

    if parsed["event_type"] == "url_verification":
        return JSONResponse({"challenge": parsed["challenge"]})
    if parsed["event_type"] == "card_action":
        return JSONResponse({"msg":"ok"})
    if parsed["event_type"] not in ("event_callback", "im.message.receive_v1"):
        return JSONResponse({"msg":"ignored"})
    if not parsed["text"] or not parsed["chat_id"]:
        return JSONResponse({"msg":"no_content"})

    text = parsed["text"]
    sid = parsed["chat_id"]
    start = time.monotonic()

    # Handle /commands
    if text.startswith("/"):
        parsed_cmd = parse_command(text)
        if parsed_cmd:
            cmd_key, label = parsed_cmd
            if cmd_key == "help":
                reply = "可用指令: /coding /translate /search /analyze /default"
            else:
                cmd_info = MODES.get(cmd_key, {})
                command_mode.set(sid, cmd_info.get("intent") or "")
                reply = "已切换至" + cmd_info.get("label", cmd_key) + "模式"
        else:
            reply = "未知指令，发送 /help 查看可用指令"
        adp = await get_adapter()
        if adp:
            await adp.send(ChannelMessage(channel="feishu", target=sid, text=reply, trace_id=""))
        await log_request(
            request, 200, (time.monotonic() - start) * 1000,
            sid, "command", cmd_key if parsed_cmd else "help", "", text, reply,
        )
        return JSONResponse({"msg":"ok"})

    # Run MoA pipeline
    trace_id = new_trace_id()
    moa_event = MoAEvent(
        trace_id=trace_id, event=FsmEvent.MESSAGE_RECEIVED,
        session_id=sid, text=text,
        context={"source": "feishu_event"},
    )
    await _engine.handle_event(moa_event)
    intent, _ = await _engine.router.route(text)
    forced = command_mode.get(sid)
    if forced:
        intent = forced
    agent = get_agent(intent) or get_agent("general")
    agent_name = intent
    for name in ("coder", "general"):
        if get_agent(name) is agent:
            agent_name = name
            break
    retrieval = await _retriever.retrieve_knowledge(text)

    envelope = AgentEnvelope(
        trace_id=trace_id, session_id=sid,
        user_raw_input=text, global_summary=retrieval.context,
        history=tuple(memory.get_history(sid)),
        agent_local_slot={"intent": intent, "resource": intent},
    )
    try:
        output = await agent.execute(envelope)
    except Exception as e:
        output = "抱歉，处理消息时出错了: " + str(e)[:100]

    with open("feishu_debug.log", "a", encoding="utf-8") as f:
        f.write("REPLY_PREP: " + output[:200] + "\n")

    adapter = await get_adapter()
    if adapter:
        msg = ChannelMessage(channel="feishu", target=sid, text=output, trace_id=trace_id)
        sent = await adapter.send(msg)
        if sent:
            memory.add(sid, text, output)
        with open("feishu_debug.log", "a", encoding="utf-8") as f:
            f.write("SENT: " + str(sent) + "\n")

    await log_request(
        request, 200, (time.monotonic() - start) * 1000,
        sid, agent_name, intent, "", text, output,
    )
    return JSONResponse({"msg":"ok"})
