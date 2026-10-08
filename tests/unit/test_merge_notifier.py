"""合并审批卡片的单测。

断言：
- 按钮存在（批准/拒绝）
- trace_id/session_id 正确
- 红卡含失败 job 名
"""

from __future__ import annotations

import pytest

from app.channels.feishu_cards import ApprovalCard
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord
from apps.code_review_pipeline.notifications.merge_notifier import MergeNotifier


def _make_record(
    state: str = MergeState.AWAITING_APPROVAL.value,
    repo: str = "owner/repo",
    pr_number: int = 42,
    head_sha: str = "abc123",
    ci_state: str = "success",
    ci_failing: tuple[dict[str, str], ...] = (),
) -> MergeRecord:
    return MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
        ci_state=ci_state,
        ci_failing=ci_failing,
    )


class _FakeCardSender:
    def __init__(self) -> None:
        self.sent_cards: list[ApprovalCard] = []

    async def send_card(self, card: ApprovalCard) -> bool:
        self.sent_cards.append(card)
        return True


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
async def test_no_card_sent_without_target() -> None:
    """没有配置收件目标时不发卡片。"""
    record = _make_record()
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="")

    ok = await notifier.send_approval_card(record)

    assert not ok
    assert len(sender.sent_cards) == 0
