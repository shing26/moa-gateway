from __future__ import annotations
import json, logging
from typing import Any

logger = logging.getLogger("moa.channels.feishu_event")

def parse_feishu_event(body):
    schema = body.get("schema", "1.0")
    header = body.get("header", {}) if isinstance(body.get("header"), dict) else {}
    
    if schema == "2.0":
        event_type = header.get("event_type", "")
        challenge = body.get("event", {}).get("challenge") if isinstance(body.get("event"), dict) else None
    else:
        event_type = body.get("type", "")
        challenge = body.get("challenge")

    result = {
        "event_type": event_type,
        "challenge": challenge,
        "message_id": None, "chat_id": None,
        "sender_id": None, "text": None,
    }

    if event_type == "url_verification":
        return result

    if event_type in ("event_callback", "im.message.receive_v1"):
        event = body.get("event", {})
        if not isinstance(event, dict):
            event = {}
        msg = event.get("message", {})
        if not isinstance(msg, dict):
            msg = {}
        result["message_id"] = msg.get("message_id")
        result["chat_id"] = msg.get("chat_id")
        s = event.get("sender", {})
        if isinstance(s, dict):
            sid = s.get("sender_id", {})
            if isinstance(sid, dict):
                result["sender_id"] = sid.get("user_id", "") or sid.get("open_id", "")
        msg_type = msg.get("msg_type", "") or msg.get("message_type", "")
        raw = msg.get("content", "")
        if msg_type == "text" and raw:
            try:
                result["text"] = json.loads(raw).get("text", "")
            except Exception:
                result["text"] = raw
        return result

    if "action" in body:
        result["event_type"] = "card_action"
        result["message_id"] = body.get("open_message_id")
        result["chat_id"] = body.get("open_chat_id")
        return result

    return result
