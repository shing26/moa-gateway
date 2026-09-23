"""卡片事件的"谁点的"必须被解析出来。

此前 `parse_feishu_event` 对 v2 卡片事件只取 chat_id 与 action value，**不取 operator**
→ 审批审计只能记"批准了"，答不出"谁批准的"，而这是审批单据的基本字段之一（2026-09-23 补）。
"""

from __future__ import annotations

from app.channels.feishu_event import parse_feishu_event

V2_CARD_ACTION = {
    "schema": "2.0",
    "header": {"event_type": "card.action.trigger", "event_id": "e1", "token": "t"},
    "event": {
        "operator": {"open_id": "ou_approver_1"},
        "action": {"value": {"action": "approve", "trace_id": "tr-1"}},
        "context": {"open_chat_id": "oc_1", "open_message_id": "om_1"},
    },
}

V1_CARD_ACTION = {
    "open_id": "ou_approver_2",
    "open_chat_id": "oc_2",
    "open_message_id": "om_2",
    "action": {"value": {"action": "reject", "trace_id": "tr-2"}},
}


def test_v2_card_action_extracts_operator() -> None:
    parsed = parse_feishu_event(V2_CARD_ACTION)

    assert parsed["event_type"] == "card_action"
    assert parsed["operator_id"] == "ou_approver_1"
    assert parsed["action"]["action"] == "approve"


def test_v1_card_action_extracts_operator() -> None:
    parsed = parse_feishu_event(V1_CARD_ACTION)

    assert parsed["event_type"] == "card_action"
    assert parsed["operator_id"] == "ou_approver_2"


def test_missing_operator_is_none_not_a_crash() -> None:
    """取不到点击者时留空即可，**不能影响审批本身**。

    注意：v2 的 `event.operator` 字段路径是按 schema 2.0 写的，**未对着真实卡片点击
    验证过**（只在单测里构造过）。所以这里是"取到就记、取不到不炸"。
    """
    v2_without_operator = {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "event_id": "e1"},
        "event": {
            "action": {"value": {"action": "approve"}},
            "context": {"open_chat_id": "oc_1"},
        },
    }
    parsed = parse_feishu_event(v2_without_operator)

    assert parsed["event_type"] == "card_action"
    assert parsed["operator_id"] is None


def test_message_events_do_not_set_operator() -> None:
    """普通消息不是审批，operator_id 保持 None（发送者另有 sender_id 字段）。"""
    body = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_sender"}},
            "message": {"chat_id": "oc_9", "msg_type": "text", "content": '{"text":"hi"}'},
        },
    }
    parsed = parse_feishu_event(body)

    assert parsed["operator_id"] is None
    assert parsed["sender_id"] == "ou_sender"
