"""卡片事件的"谁点的"必须被解析出来。

此前 `parse_feishu_event` 对 v2 卡片事件只取 chat_id 与 action value，**不取 operator**
→ 审批审计只能记"批准了"，答不出"谁批准的"，而这是审批单据的基本字段之一（2026-09-23 补）。
"""

from __future__ import annotations

import json

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


# —— 2026-10-03：飞书"没回应"的真因 ——
#
# 用户发的是**富文本**（message_type="post"），而解析器只认 "text"。于是
# result["text"] 为 None，路由走到 `if not parsed["text"]: return {"msg":"no_content"}`
# 并回 **HTTP 200** —— 飞书侧看到的是"收到了但没下文"，本侧看 access log 也是 200，
# 两边都不报错，只有 `pipeline.run result` 那行日志**缺席**能证明没进 pipeline。

_REAL_POST_CONTENT = json.dumps(
    {
        "title": "",
        "content": [[{"tag": "text", "text": "记一下 本方案报价 1999 元/月 包含三次上门服务", "style": []}]],
        "content_v2": [[{"tag": "text", "text": "记一下 本方案报价 1999 元/月 包含三次上门服务", "style": []}]],
    },
    ensure_ascii=False,
)

_REAL_POST_EVENT = {
    "schema": "2.0",
    "header": {"event_type": "im.message.receive_v1", "event_id": "4af31a34", "token": "t"},
    "event": {
        "message": {
            "chat_id": "oc_5e2a48f411b10b604ca1745f9974f61c",
            "chat_type": "p2p",
            "message_type": "post",
            "message_id": "om_x100b633b2e5a0ca0c274528e43587e4",
            "content": _REAL_POST_CONTENT,
        },
        "sender": {"sender_id": {"open_id": "ou_ae3e5cc009fae3462147e15e8c271602"}},
    },
}


def test_post_message_text_is_extracted() -> None:
    parsed = parse_feishu_event(_REAL_POST_EVENT)

    assert parsed["event_type"] == "im.message.receive_v1"
    assert parsed["text"] == "记一下 本方案报价 1999 元/月 包含三次上门服务"
    assert parsed["chat_id"] == "oc_5e2a48f411b10b604ca1745f9974f61c"


def test_post_text_is_not_duplicated_across_content_and_content_v2() -> None:
    """content 与 content_v2 是同一段文字的两种形态，同时展开会让文本翻倍。"""
    parsed = parse_feishu_event(_REAL_POST_EVENT)

    assert parsed["text"].count("1999") == 1


def test_plain_text_message_still_works() -> None:
    body = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {"chat_id": "oc_1", "msg_type": "text", "content": '{"text":"hi"}'}},
    }

    assert parse_feishu_event(body)["text"] == "hi"


def test_unknown_message_type_yields_empty_text_not_a_crash() -> None:
    """图片/文件/合并转发没有可提取的文本——返回 "" 让路由回 no_content，不能抛异常。"""
    body = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {"message": {"chat_id": "oc_1", "message_type": "image", "content": '{"image_key":"k"}'}},
    }

    assert parse_feishu_event(body)["text"] == ""
