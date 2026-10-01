"""D3 核心判据：``kill -9`` worker 后任务仍能跑完（真 Redis + 真 PostgreSQL）。

计划里的原话："``kill -9`` worker → 重启 → ``XAUTOCLAIM`` 认领 → 任务跑完"。

这里的"kill -9"这样模拟：让第一个 worker 认领消息、推进到 running 状态，
然后**在 ack 之前**直接丢弃它（相当于进程被 SIGKILL，没有任何清理机会）。

为什么必须真库：整条链的正确性依赖三件互相咬合的事实——

1. Redis 只有在**未 ack** 时才把消息留在 PEL 里等 XAUTOCLAIM；
2. PG 的条件 UPDATE 只让一个认领成功（第二个 worker 认领失败）；
3. 因此崩溃点上不同，重跑的结果不同。

任一环用 mock 替换，测到的都是"我写的 mock 的行为"，不是系统行为。
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio

from app.worker import TaskWorker
from apps.code_review_pipeline.storage.review_store import (
    PostgresReviewStore,
    ReviewRecord,
    build_review_store,
)
from apps.code_review_pipeline.task_queue import TaskQueue
from apps.code_review_pipeline.task_state import TaskState

REDIS_URL = "redis://localhost:6380/0"
DSN = "postgresql://gateway:gateway@localhost:5433/gateway"


@pytest_asyncio.fixture
async def pg_store() -> AsyncIterator[PostgresReviewStore]:
    from app.config import settings

    settings.vector_db_dsn = DSN
    try:
        store = build_review_store()
    except Exception as exc:  # noqa: BLE001 - 连不上库不是测试失败
        pytest.skip(f"PostgreSQL 不可用（{type(exc).__name__}）")
    if not isinstance(store, PostgresReviewStore):
        pytest.skip("拿到的是内存 store，说明 DSN 没生效")
    try:
        yield store
    finally:
        settings.vector_db_dsn = ""


@pytest_asyncio.fixture
async def queue() -> AsyncIterator[TaskQueue]:
    redis_mod = pytest.importorskip("redis.asyncio")
    client = redis_mod.from_url(REDIS_URL, decode_responses=False)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        pytest.skip(f"Redis 不可用（{type(exc).__name__}）")
    tag = uuid.uuid4().hex[:8]
    q = TaskQueue(client, stream=f"moa:test:{tag}", group=f"grp:{tag}")
    await q.ensure_group()
    try:
        yield q
    finally:
        await client.delete(q._stream)
        await client.aclose()


def _seed(store: PostgresReviewStore, tag: str) -> tuple[str, int, str]:
    identity = (f"org-{tag}/app", 7, f"sha-{tag}")
    repo, pr, sha = identity
    store.save(
        ReviewRecord(
            trace_id=f"cr_{repo}#{pr}@{sha}",
            repo=repo,
            pr_number=pr,
            head_sha=sha,
            author="tester",
            findings_count=0,
            need_human_review=True,
            raw={"title": "t", "base_sha": "b", "html_url": "h", "diff_url": "d"},
        )
    )
    return identity


class _HardCrash(BaseException):
    """模拟 SIGKILL：连 ``except Exception`` 都接不住，因此没有任何清理机会。"""


@pytest.mark.redis
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_task_survives_worker_crash_before_ack(
    pg_store: PostgresReviewStore, queue: TaskQueue
) -> None:
    """D3 判据：worker 崩在 ack 之前，重启后任务仍能跑完。

    崩溃点选在**认领之后、状态落库之前**——这是最容易丢任务的窗口：消息已进 PEL，
    而任务行已经是 running。若恢复逻辑只认 queued 态，这个任务就会永久卡在
    running，没人再碰它。
    """
    tag = uuid.uuid4().hex[:8]
    identity = _seed(pg_store, tag)
    await queue.enqueue(*identity)

    crashed_runs: list[str] = []

    async def _crashing(_msg: object) -> str:
        crashed_runs.append("ran")
        raise _HardCrash("simulated SIGKILL")

    worker_a = TaskWorker(queue, pg_store, _crashing, consumer="worker-A", reclaim_min_idle_ms=0)
    with pytest.raises(_HardCrash):
        await worker_a.run_once(block_ms=100)

    assert pg_store.get_task_state(identity) == TaskState.RUNNING.value
    assert await queue.pending_count() == 1, "崩溃后消息必须留在 PEL 里等认领"

    # 重启：新 worker 认领同一条消息，这次让它正常跑完。
    done_runs: list[str] = []

    async def _ok(_msg: object) -> str:
        done_runs.append("ran")
        return TaskState.WAITING_APPROVAL.value

    worker_b = TaskWorker(queue, pg_store, _ok, consumer="worker-B", reclaim_min_idle_ms=0)
    handled = await worker_b.run_once(block_ms=100)

    assert handled == 1, "重启后的 worker 没有接管崩溃遗留的任务"
    assert pg_store.get_task_state(identity) == TaskState.WAITING_APPROVAL.value
    assert await queue.pending_count() == 0, "处理完必须 ack，否则还会被第三次重投"


@pytest.mark.redis
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_crash_before_claim_loses_nothing(
    pg_store: PostgresReviewStore, queue: TaskQueue
) -> None:
    """崩在认领之前：任务仍是 queued，重启后正常被认领执行。"""
    tag = uuid.uuid4().hex[:8]
    identity = _seed(pg_store, tag)
    await queue.enqueue(*identity)

    async def _crashing(_msg: object) -> str:
        raise _HardCrash("crashed before doing anything")

    worker_a = TaskWorker(queue, pg_store, _crashing, consumer="worker-A", reclaim_min_idle_ms=0)
    with pytest.raises(_HardCrash):
        await worker_a.run_once(block_ms=100)
    # 认领发生在 processor 之前，所以这里是 running；关键是消息还在 PEL。
    assert await queue.pending_count() == 1

    ran: list[str] = []

    async def _ok(_msg: object) -> str:
        ran.append("yes")
        return TaskState.WAITING_APPROVAL.value

    worker_b = TaskWorker(queue, pg_store, _ok, consumer="worker-B", reclaim_min_idle_ms=0)
    await worker_b.run_once(block_ms=100)
    assert ran == ["yes"]
    assert pg_store.get_task_state(identity) == TaskState.WAITING_APPROVAL.value
