"""合并执行器的单测。

覆盖四种场景：
1. 批准成功
2. sha 已变（409）
3. 冲突（405 / dirty）
4. 拒绝
"""

from __future__ import annotations

import pytest

from apps.code_review_pipeline.merge_executor import MergeExecutor
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import MergeOutcome


def _make_record(
    repo: str = "owner/repo",
    pr_number: int = 42,
    head_sha: str = "abc123",
    state: str = MergeState.AWAITING_APPROVAL.value,
) -> MergeRecord:
    return MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
    )


class _FakeGitHub:
    def __init__(
        self,
        pr_info: dict | None = None,
        merge_outcome: MergeOutcome | None = None,
    ) -> None:
        self._pr_info = pr_info or {"mergeable": True, "mergeable_state": "clean"}
        self._merge_outcome = merge_outcome or MergeOutcome(
            merged=True, status="merged", sha="def456"
        )
        self.get_pr_calls: list[tuple[str, int]] = []
        self.merge_calls: list[tuple[str, int, str]] = []

    async def get_pr(self, repo, pr_number: int) -> dict:
        self.get_pr_calls.append((repo.owner, pr_number))
        return self._pr_info

    async def merge_pr(self, repo, pr_number: int, *, sha: str = "") -> MergeOutcome:
        self.merge_calls.append((repo.owner, pr_number, sha))
        return self._merge_outcome


@pytest.mark.asyncio
async def test_approve_success() -> None:
    """批准成功：状态推进到 MERGED，返回 merged=True。"""
    store = MergeStore()
    record = _make_record()
    store.save(record)

    fake_gh = _FakeGitHub()
    executor = MergeExecutor(store, fake_gh)

    outcome = await executor.execute(record.task_key, "approve", "user_123")

    assert outcome.merged
    assert outcome.status == "merged"
    # 状态推进到 MERGED
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.MERGED.value
    assert updated.approver == "user_123"
    assert updated.merged_sha == "def456"
    # 确实调了 merge_pr
    assert len(fake_gh.merge_calls) == 1
    assert fake_gh.merge_calls[0][2] == "abc123"  # sha 参数正确


@pytest.mark.asyncio
async def test_approve_sha_mismatch_409() -> None:
    """sha 已变（409）：状态推进到 FAILED，返回 merged=False。"""
    store = MergeStore()
    record = _make_record()
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
    # 状态推进到 FAILED
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.FAILED.value
    assert "sha mismatch" in updated.failure_reason


@pytest.mark.asyncio
async def test_approve_conflict_dirty() -> None:
    """冲突（dirty）：不尝试合并，直接 FAILED。"""
    store = MergeStore()
    record = _make_record()
    store.save(record)

    fake_gh = _FakeGitHub(
        pr_info={"mergeable": False, "mergeable_state": "dirty"}
    )
    executor = MergeExecutor(store, fake_gh)

    outcome = await executor.execute(record.task_key, "approve", "user_123")

    assert not outcome.merged
    assert outcome.status == "not_mergeable"
    # 没有调 merge_pr
    assert len(fake_gh.merge_calls) == 0
    # 状态推进到 FAILED
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.FAILED.value
    assert "dirty" in updated.failure_reason


@pytest.mark.asyncio
async def test_reject() -> None:
    """拒绝：状态推进到 REJECTED，不调 merge_pr。"""
    store = MergeStore()
    record = _make_record()
    store.save(record)

    fake_gh = _FakeGitHub()
    executor = MergeExecutor(store, fake_gh)

    outcome = await executor.execute(record.task_key, "reject", "user_123")

    assert not outcome.merged
    assert outcome.status == "rejected"
    # 状态推进到 REJECTED
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.REJECTED.value
    assert updated.approver == "user_123"
    # 没有调 merge_pr
    assert len(fake_gh.merge_calls) == 0
