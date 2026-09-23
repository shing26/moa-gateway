from __future__ import annotations

import pytest

from app.channels.feishu_cards import ApprovalCard, parse_card_callback


def test_approval_card_includes_buttons():
    card = ApprovalCard(
        session_id="sess-1",
        trace_id="trace-1",
        agent_name="coder",
        intent="write_file",
        agent_output="print('hello')",
        channel="feishu",
        target="chat_123",
    )
    payload = card.to_card_payload()
    assert payload["header"]["template"] == "orange"
    assert "Agent Gateway" in payload["header"]["title"]["content"]
    elements = payload["elements"]
    actions = [e for e in elements if e.get("tag") == "action"]
    assert len(actions) == 1
    buttons = actions[0]["actions"]
    assert len(buttons) == 2
    assert buttons[0]["value"]["action"] == "approve"
    assert buttons[1]["value"]["action"] == "reject"


def test_parse_card_callback_approve():
    body = {"action": {"value": {"session_id": "sess-1", "trace_id": "trace-1", "action": "approve"}}}
    result = parse_card_callback(body)
    assert result is not None
    session_id, trace_id, action = result
    assert session_id == "sess-1"
    assert action == "approve"


def test_parse_card_callback_reject():
    body = {"action": {"value": {"session_id": "sess-2", "trace_id": "trace-2", "action": "reject"}}}
    result = parse_card_callback(body)
    assert result is not None
    assert result[2] == "reject"


def test_parse_card_callback_returns_none_for_invalid():
    assert parse_card_callback({}) is None
    assert parse_card_callback({"action": {"value": {}}}) is None


def test_approval_card_message_payload_structure():
    card = ApprovalCard(
        session_id="sess-3",
        trace_id="trace-3",
        agent_name="coder",
        intent="assistant",
        agent_output="test",
        channel="feishu",
        target="chat_789",
    )
    msg = card.to_message_payload()
    assert msg["receive_id"] == "chat_789"
    assert msg["msg_type"] == "interactive"
    assert "header" in msg["content"]


def test_notification_card_has_no_action_buttons():
    """通知形态不渲染批准/拒绝按钮。

    "notification" 给的是**没有审批落地**的链路（PR 审查报告）：那条路径从不
    ``store_hitl``（`apps/` 下 0 处），渲染按钮等于承诺一个点了必然回"已失效"
    的动作。2026-09-23 修正。
    """
    card = ApprovalCard(
        session_id="t", trace_id="t", agent_name="code-review-report",
        intent="human_in_the_loop", agent_output="需要人工复核",
        channel="feishu", target="chat", hitl_kind="notification",
    )
    payload = card.to_card_payload()

    assert [e for e in payload["elements"] if e.get("tag") == "action"] == []
    assert payload["header"]["template"] == "blue"
    assert "通知" in payload["header"]["title"]["content"]


def test_code_review_review_card_is_a_notification_not_an_approval():
    """PR 审查"需要人工复核"的卡片必须是通知形态（回归见上一条）。"""
    from apps.code_review_pipeline.notifications.feishu_notifier import (
        FeishuReviewNotifier,
        ReviewNotification,
    )

    notif = ReviewNotification(
        trace_id="tr-1", repo="acme/api", pr_number=7, author="dev",
        changed_files=3, overall_need_human_review=True,
        findings_by_severity={"critical": 1}, report=None,
    )
    card = FeishuReviewNotifier._build_review_card(notif)

    assert card.hitl_kind == "notification"
    assert [e for e in card.to_card_payload()["elements"] if e.get("tag") == "action"] == []
    assert "不支持在此批准/拒绝" in card.agent_output
