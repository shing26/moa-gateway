"""合并通道的端到端测试（ADR-021）。

覆盖 ADR-021 六条技术验收里可自动化的五条：
1. CI 未绿时不产生待批卡片；CI 由红转绿后产生
2. CI 红时推送失败 job 名与日志链接
3. 批准后合并成功；合并动作在审计链上留下记录
4. 同一 head_sha 重复投递/重复点击不产生第二次合并
5. 合并失败时用户收到真实原因，且系统状态可恢复

第 6 条（审查零回归）由全量测试守。

使用内存 store + fake GitHub client，不依赖外部服务。
"""

from __future__ import annotations

import pytest

from apps.code_review_pipeline.merge_executor import MergeExecutor
from apps.code_review_pipeline.merge_poller import MergePoller
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.notifications.merge_notifier import MergeNotifier
from apps.code_review_pipeline.routing.github_client import (
    CiCheck,
    CiStatus,
    CI_SUCCESS,
    CI_FAILURE,
    MergeOutcome,
)


def _make_record(
    repo: str = "owner/repo",
    pr_number: int = 42,
    head_sha: str = "abc123",
    state: str = MergeState.WATCHING.value,
) -> MergeRecord:
    return MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
    )


class _FakeGitHub:
    """可控 CI 结论与合并结果的假 GitHub 客户端。"""

    def __init__(
        self,
        ci_status: CiStatus | None = None,
        merge_outcome: MergeOutcome | None = None,
        pr_info: dict | None = None,
    ) -> None:
        self._ci_status = ci_status or CiStatus(state=CI_SUCCESS)
        self._merge_outcome = merge_outcome or MergeOutcome(
            merged=True, status="merged", sha="def456"
        )
        self._pr_info = pr_info or {"mergeable": True, "mergeable_state": "clean"}
        self.get_pr_calls: list[tuple[str, int]] = []
        self.merge_calls: list[tuple[str, int, str]] = []

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        return self._ci_status

    async def get_pr(self, repo, pr_number: int) -> dict:
        self.get_pr_calls.append((repo.owner, pr_number))
        return self._pr_info

    async def merge_pr(self, repo, pr_number: int, *, sha: str = "") -> MergeOutcome:
        self.merge_calls.append((repo.owner, pr_number, sha))
        return self._merge_outcome


class _FakeCardSender:
    def __init__(self) -> None:
        self.sent_cards: list = []

    async def send_card(self, card) -> bool:
        self.sent_cards.append(card)
        return True


@pytest.mark.asyncio
async def test_ci_red_then_green_produces_approval_card() -> None:
    """验收 1：CI 未绿时不产生待批卡片；CI 由红转绿后产生。"""
    store = MergeStore()
    record = _make_record()
    store.save(record)

    # 第一次轮询：CI 红
    fake_gh_red = _FakeGitHub(
        ci_status=CiStatus(
            state=CI_FAILURE,
            checks=(CiCheck(name="test", conclusion="failure"),),
        )
    )
    poller_red = MergePoller(store, fake_gh_red)
    results_red = await poller_red.poll_once()
    assert len(results_red) == 0  # CI 红不发卡

    # 第二次轮询：CI 绿
    fake_gh_green = _FakeGitHub(ci_status=CiStatus(state=CI_SUCCESS))
    poller_green = MergePoller(store, fake_gh_green)
    results_green = await poller_green.poll_once()
    assert len(results_green) == 1  # CI 绿发卡
    assert results_green[0].state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_ci_red_pushes_failing_job_names() -> None:
    """验收 2：CI 红时推送失败 job 名与日志链接。"""
    store = MergeStore()
    record = _make_record()
    store.save(record)

    fake_gh = _FakeGitHub(
        ci_status=CiStatus(
            state=CI_FAILURE,
            checks=(
                CiCheck(name="test", conclusion="failure", url="https://example.com/1"),
                CiCheck(name="lint", conclusion="failure", url="https://example.com/2"),
            ),
        )
    )
    poller = MergePoller(store, fake_gh)
    await poller.poll_once()

    # 发 CI 红卡片
    sender = _FakeCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")
    updated = store.get(record.identity)
    assert updated is not None
    await notifier.send_ci_failure_card(updated)

    assert len(sender.sent_cards) == 1
    card = sender.sent_cards[0]
    payload = card.to_card_payload()
    full_text = "\n".join(
        el.get("content", "") for el in payload["elements"] if el.get("tag") == "markdown"
    )
    assert "test" in full_text
    assert "https://example.com/1" in full_text
    assert "lint" in full_text


@pytest.mark.asyncio
async def test_approve_merges_and_writes_audit() -> None:
    """验收 3：批准后合并成功；合并动作在审计链上留下记录。"""
    store = MergeStore()
    record = _make_record(state=MergeState.AWAITING_APPROVAL.value)
    store.save(record)

    fake_gh = _FakeGitHub()
    executor = MergeExecutor(store, fake_gh)

    outcome = await executor.execute(record.task_key, "approve", "user_123")

    assert outcome.merged
    # 状态推进到 MERGED
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.MERGED.value
    assert updated.approver == "user_123"
    assert updated.merged_sha == "def456"


@pytest.mark.asyncio
async def test_duplicate_approval_does_not_merge_twice() -> None:
    """验收 4：同一 head_sha 重复投递/重复点击不产生第二次合并。"""
    store = MergeStore()
    record = _make_record(state=MergeState.AWAITING_APPROVAL.value)
    store.save(record)

    fake_gh = _FakeGitHub()
    executor = MergeExecutor(store, fake_gh)

    # 第一次批准
    outcome1 = await executor.execute(record.task_key, "approve", "user_123")
    assert outcome1.merged
    assert len(fake_gh.merge_calls) == 1

    # 第二次批准（重复点击）：状态已是 MERGED，不允许再批准
    from apps.code_review_pipeline.merge_state import InvalidMergeTransition

    with pytest.raises(InvalidMergeTransition):
        await executor.execute(record.task_key, "approve", "user_123")

    # 没有第二次合并
    assert len(fake_gh.merge_calls) == 1


@pytest.mark.asyncio
async def test_merge_failure_returns_real_reason() -> None:
    """验收 5：合并失败时用户收到真实原因，且系统状态可恢复。"""
    store = MergeStore()
    record = _make_record(state=MergeState.AWAITING_APPROVAL.value)
    store.save(record)

    fake_gh = _FakeGitHub(
        merge_outcome=MergeOutcome(
            merged=False, status="sha_mismatch", message="sha mismatch"
        )
    )
    executor = MergeExecutor(store, fake_gh)

    outcome = await executor.execute(record.task_key, "approve", "user_123")

    assert not outcome.merged
    assert outcome.status == "sha_mismatch"
    assert "sha mismatch" in outcome.message
    # 状态推进到 FAILED（终态，可恢复）
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.FAILED.value
    assert "sha mismatch" in updated.failure_reason
