"""合并通道 CI 轮询器的状态转换测试。

覆盖五种核心场景：
1. watching → awaiting_approval（CI 绿，发审批卡）
2. watching → ci_failed（CI 红，发失败卡）
3. ci_failed → awaiting_approval（CI 重跑变绿，发审批卡）
4. watching + CI pending/none（未出结论，**原地不动**）
5. ci_failed + CI 仍红（重复轮询，**不重复发失败卡**）
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
    CI_NONE,
    CI_PENDING,
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

    result = await poller.poll_once()

    assert len(result.approvals) == 1
    assert len(result.ci_failures) == 0
    assert result.approvals[0].state == MergeState.AWAITING_APPROVAL.value
    assert result.approvals[0].ci_state == CI_SUCCESS
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

    result = await poller.poll_once()

    # CI 红不发审批卡，但要发失败卡（验收 2）
    assert len(result.approvals) == 0
    assert len(result.ci_failures) == 1
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

    result = await poller.poll_once()

    assert len(result.approvals) == 1
    assert result.approvals[0].state == MergeState.AWAITING_APPROVAL.value
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

    result = await poller.poll_once()

    assert len(result.approvals) == 0
    assert len(result.ci_failures) == 0
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.FAILED.value
    assert "network timeout" in updated.failure_reason
    # 终态：不会再被 list_open 返回
    assert len(store.list_open()) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ci_state", [CI_PENDING, CI_NONE])
async def test_unconcluded_ci_stays_in_watching(ci_state: str) -> None:
    """CI 未出结论（pending / none）时**原地不动**，不发任何卡片。

    回归用例（2026-10-09）：此前 ``pending`` 被当成 ``CI_RED``，于是真实的 pending
    把记录推进到 ``ci_failed`` 并推失败卡片。真实 PR #1 就卡在这里。
    """
    store = MergeStore()
    record = _make_record()
    store.save(record)

    poller = MergePoller(store, _FakeGitHub(CiStatus(state=ci_state)))
    result = await poller.poll_once()

    assert len(result.approvals) == 0
    assert len(result.ci_failures) == 0
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.WATCHING.value


@pytest.mark.asyncio
async def test_repeated_red_does_not_re_notify() -> None:
    """CI 持续红时，每一轮轮询不再重复发失败卡片（否则每 60s 一条消息）。"""
    store = MergeStore()
    record = _make_record(state=MergeState.CI_FAILED.value)
    store.save(record)

    poller = MergePoller(
        store,
        _FakeGitHub(
            CiStatus(
                state=CI_FAILURE,
                checks=(CiCheck(name="test", conclusion="failure"),),
            )
        ),
    )
    result = await poller.poll_once()

    assert len(result.approvals) == 0
    assert len(result.ci_failures) == 0


@pytest.mark.asyncio
async def test_awaiting_approval_green_does_not_raise_or_renotify() -> None:
    """卡片已经发过的记录再看到绿：不重发，更不该抛异常。"""
    store = MergeStore()
    record = _make_record(state=MergeState.AWAITING_APPROVAL.value)
    store.save(record)

    poller = MergePoller(store, _FakeGitHub(CiStatus(state=CI_SUCCESS)))
    result = await poller.poll_once()

    assert len(result.approvals) == 0
    assert len(result.ci_failures) == 0
    updated = store.get(record.identity)
    assert updated is not None
    assert updated.state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_awaiting_approval_does_not_starve_other_records() -> None:
    """回归（2026-10-09）：一条待批记录曾让整轮轮询抛异常，饿死其他 PR。"""
    store = MergeStore()
    waiting = _make_record(
        pr_number=1, head_sha="aaa", state=MergeState.AWAITING_APPROVAL.value
    )
    watching = _make_record(pr_number=2, head_sha="bbb")
    store.save(waiting)
    store.save(watching)

    poller = MergePoller(store, _FakeGitHub(CiStatus(state=CI_SUCCESS)))
    result = await poller.poll_once()

    assert [r.pr_number for r in result.approvals] == [2]
