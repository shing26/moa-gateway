from __future__ import annotations

import json

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


def test_card_renders_applicant_and_reason_when_present() -> None:
    """审批单据的两个基本字段要出现在卡片上：谁申请的、为什么（2026-09-23 补）。"""
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="coder", intent="coding",
        agent_output="out", channel="feishu", target="chat",
        applicant="ou_applicant", reason="policy.compliance.no_price_commitment",
    )
    texts = [e.get("content", "") for e in card.to_card_payload()["elements"]]

    assert any("申请人" in t and "ou_applicant" in t for t in texts)
    assert any("事由" in t and "no_price_commitment" in t for t in texts)


def test_card_omits_blank_applicant_and_reason() -> None:
    """为空时不渲染对应行——既有卡片的外观不能变。"""
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="coder", intent="coding",
        agent_output="out", channel="feishu", target="chat",
    )
    texts = [e.get("content", "") for e in card.to_card_payload()["elements"]]

    assert not any("申请人" in t for t in texts)
    assert not any("事由" in t for t in texts)


@pytest.mark.parametrize(
    "hitl_kind",
    ["merge_approval", "notification", "merge_resolved_merged"],
)
def test_d2_a13_card_body_has_no_bare_backtick_lines(hitl_kind: str) -> None:
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="merge_gate",
        intent="merge_approval", agent_output="line one\nline two",
        channel="feishu", target="chat", hitl_kind=hitl_kind,
    )
    payload = card.to_card_payload()
    body = "\n".join(
        el.get("content", "")
        for el in payload["elements"]
        if el.get("tag") == "markdown"
    )

    assert payload["header"]["title"]["content"]
    assert "line one" in body
    assert "```" in body
    assert not [
        line
        for line in body.splitlines()
        if line.startswith("`") and not line.startswith("```")
    ]


def test_d2_a14_inner_fences_are_neutralized() -> None:
    original = "before\n```\ninside\n````\nafter"
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="merge_gate",
        intent="merge_approval", agent_output=original,
        channel="feishu", target="chat", hitl_kind="merge_ci_failure",
    )
    markdown = next(
        el["content"]
        for el in card.to_card_payload()["elements"]
        if el.get("tag") == "markdown" and "失败详情" in el.get("content", "")
    )
    lines = markdown.split("\n")

    assert len([line for line in lines if line.startswith("```")]) == 2
    assert lines[1] == "```"
    assert lines[-1] == "```"
    assert "\n".join(lines[2:-1]).replace("\u200b", "") == original


def test_d2_a14_truncation_precedes_neutralization() -> None:
    original = "x" * 1998 + "```"
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="merge_gate",
        intent="merge_approval", agent_output=original,
        channel="feishu", target="chat", hitl_kind="merge_ci_failure",
    )
    markdown = next(
        el["content"]
        for el in card.to_card_payload()["elements"]
        if el.get("tag") == "markdown" and "失败详情" in el.get("content", "")
    )
    inner = "\n".join(markdown.split("\n")[2:-1])

    assert "\u200b" not in inner
    assert inner == original[:2000]


def test_d2_a15_plain_body_payload_matches_v13() -> None:
    output = "line one\n`single`\n  indented  \nline two"
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="merge_gate",
        intent="merge_approval", agent_output=output,
        channel="feishu", target="chat",
    )
    expected = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Agent Gateway - 人工审批请求"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": "**Agent**: merge_gate"},
            {"tag": "markdown", "content": "**Intent**: merge_approval"},
            {"tag": "markdown", "content": "**Trace**: t"},
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"**Agent Output**:\n```\n{output}\n```",
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准"},
                        "value": {
                            "action": "approve",
                            "session_id": "s",
                            "trace_id": "t",
                        },
                        "type": "primary",
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "value": {
                            "action": "reject",
                            "session_id": "s",
                            "trace_id": "t",
                        },
                        "type": "danger",
                    },
                ],
            },
        ],
    }

    assert json.dumps(
        card.to_card_payload(), ensure_ascii=False
    ) == json.dumps(expected, ensure_ascii=False)


@pytest.mark.parametrize(
    "hitl_kind",
    ["merge_approval", "notification", "merge_resolved_merged"],
)
def test_d2_a15_plain_bodies_render_for_all_kinds(hitl_kind: str) -> None:
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="merge_gate",
        intent="merge_approval", agent_output="plain body",
        channel="feishu", target="chat", hitl_kind=hitl_kind,
    )
    payload = card.to_card_payload()
    body = "\n".join(
        el.get("content", "")
        for el in payload["elements"]
        if el.get("tag") == "markdown"
    )

    assert "plain body" in body
    assert "\u200b" not in body
    assert payload["header"]["title"]["content"]


class _FakeAuth:
    """只实现 send_card 用到的那一个方法；不该被调用的路径一调就炸。"""

    def __init__(self) -> None:
        self.config = type("C", (), {"base_url": "https://open.feishu.cn/open-apis"})()
        self.token_calls = 0

    async def get_token(self) -> str:
        self.token_calls += 1
        raise AssertionError("非飞书渠道不应该走到取 token")


@pytest.mark.asyncio
async def test_non_feishu_channel_is_not_sent() -> None:
    # web 渠道的卡片**不发**——它没有飞书目的地。
    #
    # 回归（2026-10-02）：send_card 此前没有渠道判断，而 dashboard 的
    # /dashboard/api/chat 是 pipeline.run(..., channel="web", target=sid)，
    # target 是 session id；send_card 又固定用 receive_id_type=chat_id，
    # 于是 session id 被当成飞书会话发出去，实测报 invalid receive_id。
    #
    # 断言的是"没去取 token"而不只是返回 False：取不到 token 同样返回 False，
    # 那样凭据一坏这条用例也绿，它就验不到守卫本身了。
    from app.channels.feishu_cards import FeishuCardSender

    auth = _FakeAuth()
    sender = FeishuCardSender(auth)  # type: ignore[arg-type]
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="coder", intent="coding",
        agent_output="out", channel="web", target="web:sess-1",
    )
    assert await sender.send_card(card) is False
    assert auth.token_calls == 0


@pytest.mark.asyncio
async def test_feishu_channel_still_attempts_to_send() -> None:
    # 守卫不能误伤飞书渠道：它应该真的去取 token（即真的去发）。
    from app.channels.feishu_cards import FeishuCardSender

    auth = _FakeAuth()
    sender = FeishuCardSender(auth)  # type: ignore[arg-type]
    card = ApprovalCard(
        session_id="s", trace_id="t", agent_name="coder", intent="coding",
        agent_output="out", channel="feishu", target="oc_abc",
    )
    await sender.send_card(card)
    assert auth.token_calls == 1, "飞书渠道应该走到取 token"


@pytest.mark.asyncio
async def test_feishu_card_update_uses_message_patch_api(monkeypatch) -> None:
    from app.channels.feishu_cards import FeishuCardSender

    class _Auth:
        config = type("C", (), {"base_url": "https://open.feishu.cn/open-apis"})()

        async def get_token(self) -> str:
            return "tenant-token"

    calls: list[dict] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 0}

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def patch(self, url: str, **kwargs) -> _Response:
            calls.append({"url": url, **kwargs})
            return _Response()

    monkeypatch.setattr(
        "app.channels.feishu_cards.httpx.AsyncClient",
        _Client,
    )
    sender = FeishuCardSender(_Auth())  # type: ignore[arg-type]
    card = ApprovalCard(
        session_id="s",
        trace_id="t",
        agent_name="code-review-report",
        intent="review_feedback",
        agent_output="accepted",
        channel="feishu",
        target="",
    )

    assert await sender.update_card("om_123", card) is True
    assert calls[0]["url"].endswith("/im/v1/messages/om_123")
    assert calls[0]["headers"]["Authorization"] == "Bearer tenant-token"


def _message_sender(monkeypatch, response: dict) -> tuple:
    from app.channels.feishu_cards import FeishuCardSender

    class _Auth:
        config = type("C", (), {"base_url": "https://open.feishu.cn/open-apis"})()

        async def get_token(self) -> str:
            return "tenant-token"

    calls: list[dict] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return response

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url: str, **kwargs) -> _Response:
            calls.append({"url": url, **kwargs})
            return _Response()

    monkeypatch.setattr("app.channels.feishu_cards.httpx.AsyncClient", _Client)
    card = ApprovalCard(
        session_id="s",
        trace_id="t",
        agent_name="merge_gate",
        intent="merge_approval",
        agent_output="out",
        channel="feishu",
        target="oc_abc",
    )
    return FeishuCardSender(_Auth()), card, calls


@pytest.mark.asyncio
async def test_send_card_with_id_returns_feishu_message_id(monkeypatch) -> None:
    """T2：发送成功时返回飞书消息 id。"""
    sender, card, calls = _message_sender(
        monkeypatch, {"code": 0, "data": {"message_id": "om_sent_1"}}
    )

    assert await sender.send_card_with_id(card) == "om_sent_1"
    assert calls[0]["url"].endswith("/im/v1/messages")


@pytest.mark.asyncio
async def test_send_card_bool_contract_is_unchanged(monkeypatch) -> None:
    """T2：send_card 仍是 bool，内部复用 send_card_with_id。"""
    sender, card, _calls = _message_sender(
        monkeypatch, {"code": 0, "data": {"message_id": "om_sent_2"}}
    )

    assert await sender.send_card(card) is True


@pytest.mark.asyncio
async def test_send_card_with_id_returns_empty_on_business_error(monkeypatch) -> None:
    """T2：飞书业务错误时返回空串，send_card 仍为 False。"""
    sender, card, _calls = _message_sender(
        monkeypatch, {"code": 230001, "msg": "invalid receive_id"}
    )

    assert await sender.send_card_with_id(card) == ""
    assert await sender.send_card(card) is False


@pytest.mark.parametrize(
    ("hitl_kind", "title", "template"),
    [
        (
            "push_resolved_expired",
            "Agent Gateway - 推送审批已失效",
            "orange",
        ),
        (
            "push_resolved_approved",
            "Agent Gateway - 推送已批准",
            "green",
        ),
        (
            "push_resolved_rejected",
            "Agent Gateway - 推送被拒绝",
            "red",
        ),
    ],
)
def test_push_resolved_cards_are_terminal_without_buttons(
    hitl_kind: str, title: str, template: str
) -> None:
    """T12：三个推送终态卡无按钮，标题与模板符合 R4。"""
    card = ApprovalCard(
        session_id="delivery:run-1",
        trace_id="hitl-1",
        agent_name="delivery_push_gate",
        intent="push_approval",
        agent_output="终态",
        channel="feishu",
        target="oc_chat",
        hitl_kind=hitl_kind,
    )

    payload = card.to_card_payload()
    assert payload["header"]["title"]["content"] == title
    assert payload["header"]["template"] == template
    assert [e for e in payload["elements"] if e.get("tag") == "action"] == []


def test_existing_merge_resolved_rendering_is_unchanged() -> None:
    """T12：新增 kind 不能改变既有终态卡的标题与模板。"""
    card = ApprovalCard(
        session_id="s",
        trace_id="t",
        agent_name="merge_gate",
        intent="merge_approval",
        agent_output="⏳ 审批已过期，已重发新卡",
        channel="feishu",
        target="chat",
        hitl_kind="merge_resolved_failed",
    )

    payload = card.to_card_payload()
    assert payload["header"]["title"]["content"] == "Agent Gateway - 合并失败"
    assert payload["header"]["template"] == "red"
    assert [e for e in payload["elements"] if e.get("tag") == "action"] == []
