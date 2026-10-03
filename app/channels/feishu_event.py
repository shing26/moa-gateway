from __future__ import annotations
import json, logging
from typing import Any

logger = logging.getLogger("moa.channels.feishu_event")


def _flatten_post(node: Any, out: list[str]) -> None:
    """把 post（富文本）的嵌套结构压平成纯文本片段。

    post 的 content 是二维数组：``[[{tag, text, style}, ...], ...]``，其中
    ``tag`` 为 ``text`` / ``a``（超链接，text 是显示名）。2026-10-03 实测用户发的是
    ``message_type="post"``，而解析器此前只认 ``text``，于是 text 取到 None，
    路由直接回 ``{"msg": "no_content"}`` + HTTP 200 —— 飞书侧表现为"没回应"。
    """
    if isinstance(node, list):
        for item in node:
            _flatten_post(item, out)
    elif isinstance(node, dict):
        tag = node.get("tag", "")
        if tag in ("text", "a") and isinstance(node.get("text"), str):
            out.append(node["text"])
        # 继续下钻：md / code_block 之类也可能嵌套 text
        for key in ("content", "content_v2"):
            if key in node:
                _flatten_post(node[key], out)
    elif isinstance(node, str):
        out.append(node)


def _extract_text(msg_type: str, raw: Any) -> str:
    """按消息类型取纯文本。取不到就返回 ""，让路由回 no_content 而不是崩。"""
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except Exception:
            # 非 JSON：直接当纯文本用（text 类型偶尔如此）
            return raw.strip() if msg_type == "text" else ""
    else:
        payload = raw

    if not isinstance(payload, dict):
        return ""

    if msg_type == "post":
        # content 与 content_v2 是同一段文字的两种形态，同时展开会把文本重复一遍
        for key in ("content_v2", "content"):
            if key in payload:
                out: list[str] = []
                _flatten_post(payload[key], out)
                joined = " ".join(p.strip() for p in out if p.strip())
                if joined:
                    return joined
        return ""

    text = payload.get("text")
    if isinstance(text, str):
        return text.strip()
    # 未见过的类型（image / file / audio / merge_forward…）：不猜，返回空
    return ""


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
        if raw:
            result["text"] = _extract_text(msg_type, raw)
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
