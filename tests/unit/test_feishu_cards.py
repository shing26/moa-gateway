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
    assert payload["header"]["title"]["content"] == "MoA Engine — 人工审批请求"
    elements = payload["elements"]
    actions = [e for e in elements if e.get("tag") == "action"]
    assert len(actions) == 1
    buttons = actions[0]["actions"]
    assert len(buttons) == 2
    assert buttons[0]["value"]["action"] == "approve"
    assert buttons[1]["value"]["action"] == "reject"
    assert buttons[0]["value"]["session_id"] == "sess-1"
    assert buttons[0]["value"]["trace_id"] == "trace-1"


def test_approval_card_truncates_long_output():
    long_output = "x" * 3000
    card = ApprovalCard(
        session_id="sess-2",
        trace_id="trace-2",
        agent_name="general",
        intent="assistant",
        agent_output=long_output,
        channel="feishu",
        target="chat_456",
    )
    payload = card.to_card_payload()
    elements = payload["elements"]
    content_blocks = [e for e in elements if e.get("tag") == "markdown"]
    assert any(long_output[:50] in e["content"] for e in content_blocks)


def test_parse_card_callback_approve():
    body = {
        "action": {
            "value": {
                "session_id": "sess-1",
                "trace_id": "trace-1",
                "action": "approve",
            }
        }
    }
    result = parse_card_callback(body)
    assert result is not None
    session_id, trace_id, action = result
    assert session_id == "sess-1"
    assert trace_id == "trace-1"
    assert action == "approve"


def test_parse_card_callback_reject():
    body = {
        "action": {
            "value": {
                "session_id": "sess-2",
                "trace_id": "trace-2",
                "action": "reject",
            }
        }
    }
    result = parse_card_callback(body)
    assert result is not None
    session_id, trace_id, action = result
    assert action == "reject"


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
    msg = card.to_message_payload("fake_token")
    assert msg["receive_id"] == "chat_789"
    assert msg["msg_type"] == "interactive"
    assert "header" in msg["content"]
