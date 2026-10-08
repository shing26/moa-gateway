"""合并通道 CI 轮询器的状态转换测试。

覆盖三种核心场景：
1. watching → awaiting_approval（CI 绿，需要发卡）
2. watching → ci_failed（CI 红，不需要发卡）
3. ci_failed → awaiting_approval（CI 重跑变绿，需要发卡）
"""

from __future__ import annotations

import pytest

from apps.code_review_pipeline.merge_poller import MergePoller
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import (
    CiCheck,
    CiStatus,
    CI_SUCCESS,
    CI_FAILURE,
)


class _FakeGitHub:
    """可控 CI 结论的假 GitHub 客户端。"""

    def __init__(self, ci_status: CiStatus) -> None:
        self._ci_status = ci_status
        self.calls: list[tuple[str, str]] = []

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        self.calls.append((repo.owner, ref))
        return self._ci_status


def _make_record(
    state: str = MergeState.WATCHING.value,
    repo: str = "owner/repo",
    pr_number: int = 42,
    head_sha: str = "abc123",
) -> MergeRecord:
    return MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
    )


@pytest.mark.asyncio
async def test_watching_to_awaiting_approval_when_ci_green() -> None:
    """CI 绿：watching → awaiting_approval，返回需要发卡的记录。"""
    store = MergeStore()
    record = _make_record()
    store.save(record)

    fake_gh = _FakeGitHub(CiStatus(state=CI_SUCCESS))
    poller = MergePoller(store, fake_gh)

    results = await poller.poll_once()

    assert len(results) == 1
    assert results[0].state == MergeState.AWAITING_APPROVAL.value
    assert results[0].ci_state == CI_SUCCESS
    # 状态确实推进了
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_watching_to_ci_failed_when_ci_red() -> None:
    """CI 红：watching → ci_failed，不返回发卡记录。"""
    store = MergeStore()
    record = _make_record()
    store.save(record)

    fake_gh = _FakeGitHub(
        CiStatus(
            state=CI_FAILURE,
            checks=(CiCheck(name="test", conclusion="failure"),),
        )
    )
    poller = MergePoller(store, fake_gh)

    results = await poller.poll_once()

    # CI 红不发卡
    assert len(results) == 0
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.CI_FAILED.value
    assert updated.ci_state == CI_FAILURE
    # 失败 job 名被记录
    assert len(updated.ci_failing) == 1
    assert updated.ci_failing[0]["name"] == "test"


@pytest.mark.asyncio
async def test_ci_failed_to_awaiting_approval_when_ci_rerun_green() -> None:
    """CI 重跑变绿：ci_failed → awaiting_approval，返回需要发卡的记录。"""
    store = MergeStore()
    record = _make_record(state=MergeState.CI_FAILED.value)
    store.save(record)

    fake_gh = _FakeGitHub(CiStatus(state=CI_SUCCESS))
    poller = MergePoller(store, fake_gh)

    results = await poller.poll_once()

    assert len(results) == 1
    assert results[0].state == MergeState.AWAITING_APPROVAL.value
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_ci_read_failure_marks_record_as_failed() -> None:
    """读 CI 失败（网络/权限）→ FAILED（终态），不静默重试。"""
    store = MergeStore()
    record = _make_record()
    store.save(record)

    class _BrokenGitHub:
        async def get_ci_status(self, repo, ref: str) -> CiStatus:
            raise RuntimeError("network timeout")

    poller = MergePoller(store, _BrokenGitHub())

    results = await poller.poll_once()

    assert len(results) == 0
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.FAILED.value
    assert "network timeout" in updated.failure_reason
    # 终态：不会再被 list_open 返回
    assert len(store.list_open()) == 0
