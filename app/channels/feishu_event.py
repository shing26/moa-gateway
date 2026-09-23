from __future__ import annotations
import json, logging
from typing import Any

logger = logging.getLogger("moa.channels.feishu_event")

def parse_feishu_event(body):
    schema = body.get("schema", "1.0")
    header = body.get("header", {}) if isinstance(body.get("header"), dict) else {}

    if schema == "2.0":
        event_type = header.get("event_type", "")
        event = body.get("event", {})
        if isinstance(event, dict):
            challenge = event.get("challenge")
        else:
            # v2 url_verification 可能直接把 challenge 放在 body 顶层
            challenge = body.get("challenge")
    else:
        event_type = body.get("type", "")
        challenge = body.get("challenge")

    result = {
        "event_type": event_type,
        "challenge": challenge,
        "message_id": None, "chat_id": None,
        "sender_id": None, "text": None,
        # 审批人（卡片点击者）——"谁批准了"以前在数据层答不上（2026-09-23 补）
        "operator_id": None,
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
        # v1 卡片动作把点击者放在顶层
        result["operator_id"] = body.get("open_id") or body.get("user_id") or None
        return result

    if event_type == "card.action.trigger":
        result["event_type"] = "card_action"
        event = body.get("event", {})
        result["message_id"] = event.get("context", {}).get("open_message_id")
        result["chat_id"] = event.get("context", {}).get("open_chat_id")
        result["action"] = event.get("action", {}).get("value", {})
        # v2 卡片动作：点击者在 event.operator（按 schema 2.0）。
        # ⚠️ 该字段路径未对着**真实卡片点击**验证过（只在单测里构造过）——
        # 拿到真实回调日志后应核对一次；取不到时留空，不影响审批本身。
        operator = event.get("operator", {})
        if isinstance(operator, dict):
            result["operator_id"] = operator.get("open_id") or operator.get("user_id") or None
        return result

    return result
