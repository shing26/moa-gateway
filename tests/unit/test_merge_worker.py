"""合并 worker 一轮内的顺序：CI 轮询 → 卡片 → GitHub 评论审批（ADR-040）。

评论审批必须挂在既有 CI 轮询之后，这样本轮刚进入 ``awaiting_approval`` 的记录
也能在同一轮读到已有评论；同时它不能改变 CI 轮询与卡片发送的既有语义。
"""

from __future__ import annotations

import pytest

from app.merge_worker import MergeWorker
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import CI_SUCCESS, CiStatus


class _OrderedGitHub:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        self._events.append("ci")
        return CiStatus(state=CI_SUCCESS)


class _OrderedNotifier:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def send_approval_card(self, record) -> None:
        self._events.append("card")

    async def send_ci_failure_card(self, record) -> None:
        self._events.append("failure")


class _OrderedCommentApproval:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def poll_once(self) -> tuple:
        self._events.append("comment")
        return ()


class _CountingHeartbeat:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls = 0
        self._events = events

    async def __call__(self) -> None:
        self.calls += 1
        if self._events is not None:
            self._events.append("heartbeat")


async def _noop_heartbeat() -> None:
    return None


@pytest.mark.asyncio
async def test_comment_approval_runs_after_ci_poll_and_card() -> None:
    events: list[str] = []
    store = MergeStore()
    store.save(
        MergeRecord(
            repo="owner/repo",
            pr_number=7,
            head_sha="sha7",
            state=MergeState.WATCHING.value,
            bundle_hash="abc123",
        )
    )
    worker = MergeWorker(
        store=store,
        github=_OrderedGitHub(events),
        notifier=_OrderedNotifier(events),  # type: ignore[arg-type]
        comment_approval=_OrderedCommentApproval(events),
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert events == ["ci", "card", "comment"]
    assert store.list_open()[0].state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_heartbeat_is_written_once_at_end_of_successful_round() -> None:
    """T1：一轮成功后写一次心跳，且写在评论审批之后（证明整轮真的跑完）。"""
    events: list[str] = []
    heartbeat = _CountingHeartbeat(events)
    worker = MergeWorker(
        store=MergeStore(),
        github=_OrderedGitHub(events),
        notifier=_OrderedNotifier(events),  # type: ignore[arg-type]
        comment_approval=_OrderedCommentApproval(events),
        heartbeat=heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert heartbeat.calls == 1
    assert events[-1] == "heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_failure_is_logged_but_does_not_break_round(caplog) -> None:
    """T1：心跳写失败只告警，轮询结果与卡片照常，异常不外泄。"""
    events: list[str] = []

    async def failing_heartbeat() -> None:
        raise ConnectionError("redis down")

    store = MergeStore()
    store.save(
        MergeRecord(
            repo="owner/repo",
            pr_number=8,
            head_sha="sha8",
            state=MergeState.WATCHING.value,
            bundle_hash="abc123",
        )
    )
    worker = MergeWorker(
        store=store,
        github=_OrderedGitHub(events),
        notifier=_OrderedNotifier(events),  # type: ignore[arg-type]
        comment_approval=_OrderedCommentApproval(events),
        heartbeat=failing_heartbeat,
    )

    with caplog.at_level("WARNING", logger="moa.merge_worker"):
        await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert events == ["ci", "card", "comment"]
    assert store.list_open()[0].state == MergeState.AWAITING_APPROVAL.value
    assert "heartbeat write failed" in caplog.text
