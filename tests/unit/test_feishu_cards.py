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
    assert "MoA Engine" in payload["header"]["title"]["content"]
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
