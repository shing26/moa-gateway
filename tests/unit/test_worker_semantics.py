"""worker 的三条核心契约（D3）。

1. **ack 在状态落库之后**。顺序反了，崩溃恢复就会丢任务。
2. **重复投递不重复执行**。同一 identity 的第二条消息必须被跳过。
3. **失败不无限重投**。坏消息要 ack 掉 + 标 failed，否则 worker 卡死在一个
   永远失败的任务上。

这里用假队列 + 内存 store：queue 与 store 之间的**时序**才是要测的东西，
Redis 与 Postgres 的各自行为已由 tests/integration 覆盖。
"""

from __future__ import annotations

import os

import pytest

from app.worker import TaskWorker
from apps.code_review_pipeline.storage.review_store import ReviewRecord, ReviewStore
from apps.code_review_pipeline.task_queue import TaskMessage
from apps.code_review_pipeline.task_state import TaskState

IDENTITY = ("org/app", 7, "sha-abc")


def _message(message_id: str = "1-0") -> TaskMessage:
    repo, pr, sha = IDENTITY
    return TaskMessage(
        message_id=message_id,
        repo=repo,
        pr_number=pr,
        head_sha=sha,
        task_key=f"{repo}#{pr}@{sha}",
    )


def _record() -> ReviewRecord:
    repo, pr, sha = IDENTITY
    return ReviewRecord(
        trace_id=f"cr_{repo}#{pr}@{sha}",
        repo=repo,
        pr_number=pr,
        head_sha=sha,
        author="u",
        findings_count=0,
        need_human_review=True,
        raw={},
    )


class FakeQueue:
    """记录事件顺序的假队列：``events`` 用来断言 ack 与落库的先后。"""

    def __init__(self, batches: list[list[TaskMessage]] | None = None) -> None:
        self._batches = batches or []
        self.events: list[str] = []
        self.acked: list[str] = []
        self.reclaim_calls = 0

    async def ensure_group(self) -> bool:
        return False

    async def reclaim_stale(self, consumer: str, *, min_idle_ms: int, count: int = 10):
        self.reclaim_calls += 1
        return []

    async def read(self, consumer: str, *, block_ms: int = 5000, count: int = 1):
        if self._batches:
            self.events.append("read")
            return self._batches.pop(0)
        return []

    async def ack(self, message_id: str) -> int:
        self.events.append("ack")
        self.acked.append(message_id)
        return 1


class OrderTrackingStore(ReviewStore):
    """把状态变化记进同一条时间线，好和 ack 比顺序。"""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def transition_task(self, identity: tuple[str, int, str], action: str) -> str:
        self.events.append(f"transition:{action}")
        return super().transition_task(identity, action)


@pytest.mark.asyncio
async def test_ack_happens_after_state_is_persisted() -> None:
    """核心契约：先落库，后 ack。

    顺序反了的话，崩在两者之间会让消息已被 ack 但状态没推进——任务就此消失，
    而且没有任何错误日志。
    """
    store_events: list[str] = []
    store = OrderTrackingStore(store_events)
    store.save(_record())
    queue = FakeQueue([[_message()]])

    async def _proc(_msg: TaskMessage) -> str:
        store_events.append("processed")
        return TaskState.WAITING_APPROVAL.value

    worker = TaskWorker(queue, store, _proc, consumer="w1", reclaim_min_idle_ms=0)  # type: ignore[arg-type]
    await worker.run_once(block_ms=1)

    assert store_events == ["processed", "transition:request_approval"]
    assert queue.events == ["read", "ack"]


@pytest.mark.asyncio
async def test_duplicate_delivery_is_skipped_not_reprocessed() -> None:
    """重复投递的第二条消息必须被跳过，不重跑 processor。"""
    store = ReviewStore()
    store.save(_record())
    calls: list[TaskMessage] = []

    async def _proc(msg: TaskMessage) -> str:
        calls.append(msg)
        return TaskState.DONE.value

    queue = FakeQueue([[_message("1-0"), _message("2-0")]])
    worker = TaskWorker(queue, store, _proc, consumer="w1")  # type: ignore[arg-type]
    handled = await worker.run_once(block_ms=1)

    assert len(calls) == 1, "重复投递把任务跑了两遍"
    assert handled == 2
    assert store.get_task_state(IDENTITY) == TaskState.DONE.value
    assert sorted(queue.acked) == ["1-0", "2-0"], "跳过的消息也必须 ack，否则永远重投"


@pytest.mark.asyncio
async def test_processor_failure_marks_failed_and_acks() -> None:
    """processor 抛异常 -> 标 failed + ack。不 ack 就会无限重投这条坏消息。"""
    store = ReviewStore()
    store.save(_record())
    queue = FakeQueue([[_message()]])

    async def _boom(_msg: TaskMessage) -> str:
        raise RuntimeError("agent exploded")

    worker = TaskWorker(queue, store, _boom, consumer="w1")  # type: ignore[arg-type]
    await worker.run_once(block_ms=1)

    assert store.get_task_state(IDENTITY) == TaskState.FAILED.value
    assert queue.acked == ["1-0"]


@pytest.mark.asyncio
async def test_unknown_task_is_dropped() -> None:
    """消息指向不存在的任务行：ack 掉，不无限重投一条永远做不了事的消息。"""
    store = ReviewStore()
    queue = FakeQueue([[_message()]])

    async def _proc(_msg: TaskMessage) -> str:
        raise AssertionError("不该被调用")

    worker = TaskWorker(queue, store, _proc, consumer="w1")  # type: ignore[arg-type]
    assert await worker.run_once(block_ms=1) == 1
    assert queue.acked == ["1-0"]


@pytest.mark.asyncio
async def test_reclaim_runs_before_read_on_every_iteration() -> None:
    """回收必须在每轮开头：worker 运行中崩溃的消息也要被接管。"""
    store = ReviewStore()
    store.save(_record())
    queue = FakeQueue()

    async def _proc(_msg: TaskMessage) -> str:
        return TaskState.DONE.value

    worker = TaskWorker(queue, store, _proc, consumer="w1")  # type: ignore[arg-type]
    await worker.run_once(block_ms=1)
    await worker.run_once(block_ms=1)
    assert queue.reclaim_calls == 2, "回收只在启动时做了"


@pytest.mark.asyncio
async def test_outcome_done_completes_the_task() -> None:
    store = ReviewStore()
    store.save(_record())
    queue = FakeQueue([[_message()]])

    async def _proc(_msg: TaskMessage) -> str:
        return TaskState.DONE.value

    worker = TaskWorker(queue, store, _proc, consumer="w1")  # type: ignore[arg-type]
    await worker.run_once(block_ms=1)
    assert store.get_task_state(IDENTITY) == TaskState.DONE.value


def test_worker_default_consumer_identifies_host_and_pid() -> None:
    """consumer 名要能认出是哪个进程：排查卡在 PEL 里的任务时需要。"""
    worker = TaskWorker(FakeQueue(), ReviewStore(), None, consumer=None)  # type: ignore[arg-type]
    assert str(os.getpid()) in worker.consumer
    assert len(worker.consumer.split("-")) >= 3
