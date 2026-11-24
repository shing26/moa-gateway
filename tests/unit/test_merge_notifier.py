"""合并审批卡片的单测。

断言：
- 按钮存在（批准/拒绝）
- trace_id/session_id 正确
- 红卡含失败 job 名
"""

from __future__ import annotations

from typing import Any

import pytest

from app import deps
from app.channels.feishu_cards import ApprovalCard
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord
from apps.code_review_pipeline.notifications.merge_notifier import (
    ApprovalCardDelivery,
    MergeNotifier,
)


def _make_record(
    state: str = MergeState.AWAITING_APPROVAL.value,
    repo: str = "owner/repo",
    pr_number: int = 42,
    head_sha: str = "abc123",
    ci_state: str = "success",
    ci_failing: tuple[dict[str, str], ...] = (),
    card_message_id: str = "",
    approver: str = "",
    merged_sha: str = "",
    failure_reason: str = "",
) -> MergeRecord:
    return MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
        ci_state=ci_state,
        ci_failing=ci_failing,
        card_message_id=card_message_id,
        approver=approver,
        merged_sha=merged_sha,
        failure_reason=failure_reason,
    )


class _FakeCardSender:
    def __init__(self) -> None:
        self.sent_cards: list[ApprovalCard] = []

    async def send_card(self, card: ApprovalCard) -> bool:
        self.sent_cards.append(card)
        return True

    async def send_card_with_id(self, card: ApprovalCard) -> str:
        self.sent_cards.append(card)
        return "om_card_1"


class _FailingCardSender:
    async def send_card(self, card: ApprovalCard) -> bool:
        return False

    async def send_card_with_id(self, card: ApprovalCard) -> str:
        return ""


class _ResolvedCardSender:
    """Records in-place card updates; optionally raises like a transport failure."""

    def __init__(self, *, raises: bool = False) -> None:
        self.updates: list[tuple[str, ApprovalCard]] = []
        self.raises = raises

    async def send_card(self, card: ApprovalCard) -> bool:
        return True

    async def send_card_with_id(self, card: ApprovalCard) -> str:
        return "om_unused"

    async def update_card(self, message_id: str, card: ApprovalCard) -> bool:
        if self.raises:
            raise RuntimeError("feishu down")
        self.updates.append((message_id, card))
        return True


_PR_DETAILS = {
    "title": "feat: enrich approval card",
    "additions": 120,
    "deletions": 7,
    "changed_files": 4,
    "base": "main",
    "html_url": "https://github.com/owner/repo/pull/42",
    "login": "shing26",
}


class _FakeSessionStore:
    """最小 SessionStore 替身，只记 store_hitl / remove_hitl。"""

    def __init__(self) -> None:
        self.stored: dict[str, Any] = {}
        self.removed: list[str] = []

    def store_hitl(self, session_id: str, request: Any) -> None:
        self.stored[request.trace_id or session_id] = request

    def remove_hitl(self, hitl_id: str) -> None:
        self.removed.append(hitl_id)
        self.stored.pop(hitl_id, None)


@pytest.mark.asyncio
async def test_approval_card_registers_hitl_pending_entry() -> None:
    """发卡必须登记待批记录，否则点击只会得到"已失效"（2026-10-09 实测）。"""
    record = _make_record()
    sender = _FakeCardSender()
    store = _FakeSessionStore()
    notifier = MergeNotifier(
        card_sender=sender, default_target="chat_123", session_store=store
    )

    ok = await notifier.send_approval_card(record)

    assert ok
    request = store.stored[record.task_key]
    assert request.hitl_kind == "merge_approval"
    assert request.trace_id == record.task_key
    assert request.session_id == record.task_key


@pytest.mark.asyncio
async def test_failed_send_withdraws_pending_entry() -> None:
    """卡片没发出去就不能留下待批记录，否则那条记录永远无人点击。"""
    record = _make_record()
    store = _FakeSessionStore()
    notifier = MergeNotifier(
        card_sender=_FailingCardSender(), default_target="chat_123", session_store=store
    )

    ok = await notifier.send_approval_card(record)

    assert not ok
    assert record.task_key in store.removed
    assert store.stored == {}


@pytest.mark.asyncio
async def test_approval_card_has_buttons() -> None:
    """审批卡片必须有批准/拒绝按钮。"""
    record = _make_record()
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    ok = await notifier.send_approval_card(record)

    assert ok
    assert len(sender.sent_cards) == 1
    card = sender.sent_cards[0]
    payload = card.to_card_payload()
    # 找到 action 元素
    action_elements = [
        el for el in payload["elements"] if el.get("tag") == "action"
    ]
    assert len(action_elements) == 1
    buttons = action_elements[0]["actions"]
    assert len(buttons) == 2
    assert buttons[0]["text"]["content"] == "批准"
    assert buttons[1]["text"]["content"] == "拒绝"


@pytest.mark.asyncio
async def test_approval_card_has_correct_trace_and_session() -> None:
    """卡片的 trace_id/session_id 与记录一致。"""
    record = _make_record()
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    await notifier.send_approval_card(record)

    card = sender.sent_cards[0]
    assert card.trace_id == record.task_key
    assert card.session_id == record.task_key


@pytest.mark.asyncio
async def test_ci_failure_card_contains_failing_job_names() -> None:
    """CI 红时卡片必须列出失败的 job 名。"""
    record = _make_record(
        state=MergeState.CI_FAILED.value,
        ci_state="failure",
        ci_failing=(
            {"name": "test", "url": "https://github.com/owner/repo/runs/123"},
            {"name": "lint", "url": ""},
        ),
    )
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    ok = await notifier.send_ci_failure_card(record)

    assert ok
    card = sender.sent_cards[0]
    payload = card.to_card_payload()
    # 找到所有 markdown 元素，拼成全文
    full_text = "\n".join(
        el.get("content", "") for el in payload["elements"] if el.get("tag") == "markdown"
    )
    assert "test" in full_text
    assert "https://github.com/owner/repo/runs/123" in full_text
    assert "lint" in full_text


@pytest.mark.asyncio
async def test_ci_failure_card_has_no_action_buttons() -> None:
    """CI 红时没有可批准的东西：按钮点了必然无效，不该渲染。"""
    record = _make_record(state=MergeState.CI_FAILED.value, ci_state="failure")
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    await notifier.send_ci_failure_card(record)

    payload = sender.sent_cards[0].to_card_payload()
    assert [el for el in payload["elements"] if el.get("tag") == "action"] == []


@pytest.mark.asyncio
async def test_no_card_sent_without_target() -> None:
    """没有配置收件目标时不发卡片。"""
    record = _make_record()
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="")

    ok = await notifier.send_approval_card(record)

    assert not ok
    assert len(sender.sent_cards) == 0


@pytest.mark.asyncio
async def test_notifier_reads_deps_card_sender_at_call_time(monkeypatch) -> None:
    """T5：导入期别名会绑死 None；调用时读取才能拿到 init_feishu 之后的值。"""
    sender = _FakeCardSender()
    monkeypatch.setattr(deps, "_card_sender", sender)
    notifier = MergeNotifier(default_target="chat_123")

    ok = await notifier.send_pr_opened_card(
        _make_record(), title="t", url="https://example.test/pr/1"
    )

    assert ok
    assert len(sender.sent_cards) == 1


@pytest.mark.asyncio
async def test_init_feishu_then_new_notifier_uses_global_sender(monkeypatch) -> None:
    """T5：init_feishu() 之后新建的 notifier（未注入）能拿到全局 sender。"""
    sender = _FakeCardSender()
    monkeypatch.setattr(deps, "_card_sender", None)
    monkeypatch.setattr(deps, "_feishu_config", None)
    monkeypatch.setattr(deps.settings, "feishu_app_id", "app_id")
    monkeypatch.setattr(deps.settings, "feishu_app_secret", "app_secret")
    monkeypatch.setattr(deps, "FeishuCardSender", lambda _auth: sender)

    class _PipelineStub:
        def set_card_sender(self, _card_sender) -> None:
            return None

    monkeypatch.setattr(deps, "pipeline", _PipelineStub())
    deps.init_feishu()

    notifier = MergeNotifier(default_target="chat_123")
    ok = await notifier.send_pr_opened_card(
        _make_record(), title="t", url="https://example.test/pr/2"
    )

    assert ok
    assert sender.sent_cards


@pytest.mark.asyncio
async def test_explicit_card_sender_wins_over_deps_global(monkeypatch) -> None:
    """T5：显式注入优先。"""
    explicit = _FakeCardSender()
    global_sender = _FakeCardSender()
    monkeypatch.setattr(deps, "_card_sender", global_sender)
    notifier = MergeNotifier(card_sender=explicit, default_target="chat_123")

    ok = await notifier.send_pr_opened_card(
        _make_record(), title="t", url="https://example.test/pr/3"
    )

    assert ok
    assert len(explicit.sent_cards) == 1
    assert global_sender.sent_cards == []


@pytest.mark.asyncio
async def test_no_sender_skips_with_warning_without_raising(monkeypatch, caplog) -> None:
    """T5：app.deps._card_sender 为 None 时跳过并记 warning，不抛异常。"""
    monkeypatch.setattr(deps, "_card_sender", None)
    notifier = MergeNotifier(default_target="chat_123")

    with caplog.at_level("WARNING", logger="moa.merge_notifier"):
        ok = await notifier.send_pr_opened_card(
            _make_record(), title="t", url="https://example.test/pr/4"
        )

    assert not ok
    assert "card sender not initialized" in caplog.text


def _card_text(card: ApprovalCard) -> str:
    return "\n".join(
        el.get("content", "")
        for el in card.to_card_payload()["elements"]
        if el.get("tag") == "markdown"
    )


def _has_action_buttons(card: ApprovalCard) -> bool:
    return any(
        el.get("tag") == "action" for el in card.to_card_payload()["elements"]
    )


@pytest.mark.asyncio
async def test_approval_card_is_enriched_with_pr_details() -> None:
    """T1：富化成功时卡片含标题、变更统计、作者、链接、短 SHA 与批准后说明。"""
    record = _make_record(head_sha="deadbeef1234567890")
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    delivery = await notifier.send_approval_card(record, _PR_DETAILS)

    assert delivery.sent
    assert delivery.message_id == "om_card_1"
    card = sender.sent_cards[0]
    text = _card_text(card)
    assert "标题: feat: enrich approval card" in text
    assert "变更: 4 files  +120 −7  → main" in text
    assert "作者: shing26" in text
    assert "PR  : #42 https://github.com/owner/repo/pull/42" in text
    assert "SHA : deadbeef1234" in text
    assert "CI  : success" in text
    assert "批准后: 以 merge commit 合并到 main，并写入审计（审批人 + 时间）" in text
    assert card.hitl_kind == "merge_approval"
    assert _has_action_buttons(card)


@pytest.mark.asyncio
async def test_approval_card_falls_back_to_three_lines_without_details() -> None:
    """T1：没有富化数据时仍是旧三行，卡片照发。"""
    record = _make_record(head_sha="deadbeef1234567890")
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    delivery = await notifier.send_approval_card(record, None)

    assert delivery.sent
    assert sender.sent_cards[0].agent_output == (
        "PR: owner/repo#42\n"
        "SHA: deadbeef1234567890\n"
        "CI: success"
    )


@pytest.mark.asyncio
async def test_send_approval_card_failure_returns_empty_message_id() -> None:
    """T2：发送失败时显式返回 sent=False 且 message_id 为空。"""
    notifier = MergeNotifier(
        card_sender=_FailingCardSender(), default_target="chat_123"
    )

    delivery = await notifier.send_approval_card(_make_record(), _PR_DETAILS)

    assert isinstance(delivery, ApprovalCardDelivery)
    assert not delivery
    assert delivery.sent is False
    assert delivery.message_id == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "detail", "expected"),
    [
        (
            "merged",
            "0123456789abcdef",
            "✅ 已合并 0123456789ab（审批人 alice）",
        ),
        (
            "superseded",
            "",
            "⚠️ 本卡已作废：PR head 变了（abc123）；新记录已重建",
        ),
        (
            "failed",
            "merge conflict on main",
            "❌ 合并失败：merge conflict on main",
        ),
    ],
)
async def test_resolved_card_updates_in_place_without_buttons(
    outcome: str, detail: str, expected: str
) -> None:
    """T3：三种终态各改一次卡，文案正确且无按钮。"""
    record = _make_record(
        state=MergeState.MERGED.value if outcome == "merged" else MergeState.FAILED.value,
        ci_state="success",
        card_message_id="om_old",
        approver="alice",
        head_sha="abc123",
    )
    sender = _ResolvedCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    ok = await notifier.send_resolved_card(record, outcome, detail)

    assert ok
    assert len(sender.updates) == 1
    message_id, card = sender.updates[0]
    assert message_id == "om_old"
    assert expected in _card_text(card)
    assert not _has_action_buttons(card)


@pytest.mark.asyncio
async def test_resolved_card_without_message_id_sends_nothing() -> None:
    """T3：没有卡片消息 id 时不发新消息。"""
    sender = _ResolvedCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    ok = await notifier.send_resolved_card(
        _make_record(card_message_id=""), "merged", "0123456789abcdef"
    )

    assert not ok
    assert sender.updates == []


@pytest.mark.asyncio
async def test_rejected_resolved_card_text_and_no_buttons() -> None:
    """F2：拒绝终态文案含 🚫 已拒绝，且卡片没有审批按钮。"""
    record = _make_record(
        state=MergeState.REJECTED.value,
        card_message_id="om_old",
        approver="alice",
    )
    sender = _ResolvedCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    ok = await notifier.send_resolved_card(record, "rejected", "ou_alice")

    assert ok
    assert len(sender.updates) == 1
    _message_id, card = sender.updates[0]
    assert "🚫 已拒绝（审批人 alice）" in _card_text(card)
    assert not _has_action_buttons(card)


@pytest.mark.asyncio
async def test_resolved_card_update_exception_only_warns(caplog) -> None:
    """T3：update_card 抛异常只告警，不向外抛。"""
    sender = _ResolvedCardSender(raises=True)
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")

    with caplog.at_level("WARNING", logger="moa.merge_notifier"):
        ok = await notifier.send_resolved_card(
            _make_record(card_message_id="om_old"), "failed", "boom"
        )

    assert not ok
    assert "resolved card update failed" in caplog.text
