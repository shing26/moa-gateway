"""Redis Streams 队列的真 Redis 验证（D3）。

核心用例是 ``kill -9`` 的模拟：消息已被 XREADGROUP 投递、进入 PEL，但 worker
还没来得及 XACK 就死了。这个场景**无法用 mock 证伪**——XAUTOCLAIM 的 PEL 语义、
空闲时长判定、组内多 consumer 的归属，都是 Redis 服务端行为。所以这里用真 Redis。

compose 未起时 skip（不伪造通过）：

    docker compose -f docker-compose.dev.yml up -d redis
    uv run pytest tests/integration/test_task_queue_redis.py -q
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio

from apps.code_review_pipeline.task_queue import CONSUMER_GROUP, TaskQueue

REDIS_URL = "redis://localhost:6380/0"


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[object]:
    redis = pytest.importorskip("redis.asyncio")
    client = redis.from_url(REDIS_URL, decode_responses=False)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - Redis 不在不是测试失败
        await client.aclose()
        pytest.skip(f"Redis 不可用（{type(exc).__name__}），跳过集成用例")
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def queue(redis_client: object) -> AsyncIterator[TaskQueue]:
    """每个用例一个独立 stream 与 group，互不干扰，也不污染真实队列。"""
    tag = uuid.uuid4().hex[:8]
    q = TaskQueue(redis_client, stream=f"moa:test:{tag}", group=f"grp:{tag}")  # type: ignore[arg-type]
    await q.ensure_group()
    try:
        yield q
    finally:
        await redis_client.delete(q._stream)  # noqa: SLF001


@pytest.mark.redis
@pytest.mark.asyncio
async def test_ensure_group_is_idempotent(queue: TaskQueue) -> None:
    # queue fixture 已经建过一次组了，所以这里第一次调用就该返回 False。
    # 刻意保留这个顺序而不是新建 queue：它顺带证明了 fixture 的建组是真的生效了
    # （否则"第一次返回 False"就说不通）。
    assert await queue.ensure_group() is False, "重复建组没有被识别为已存在"


@pytest.mark.redis
@pytest.mark.asyncio
async def test_first_ensure_group_reports_created(redis_client: object) -> None:
    """全新 stream 上首次建组必须返回 True（调用方据此知道"我建了组"）。"""
    tag = uuid.uuid4().hex[:8]
    q = TaskQueue(redis_client, stream=f"moa:test:{tag}", group=f"grp:{tag}")  # type: ignore[arg-type]
    try:
        assert await q.ensure_group() is True
        assert await q.ensure_group() is False
    finally:
        await redis_client.delete(q._stream)  # noqa: SLF001


@pytest.mark.redis
@pytest.mark.asyncio
async def test_enqueue_then_read_roundtrip(queue: TaskQueue) -> None:
    await queue.enqueue("org/app", 7, "sha-abc")
    msgs = await queue.read("worker-1", block_ms=100)
    assert len(msgs) == 1
    assert msgs[0].identity == ("org/app", 7, "sha-abc")
    assert msgs[0].task_key == "org/app#7@sha-abc"


@pytest.mark.redis
@pytest.mark.asyncio
async def test_ack_moves_message_out_of_pending(queue: TaskQueue) -> None:
    """ack 的时机契约：ack 之后消息不再被任何人重投。"""
    await queue.enqueue("org/app", 7, "sha-abc")
    msgs = await queue.read("worker-1", block_ms=100)
    assert await queue.pending_count() == 1, "投递后应在 PEL 里"
    assert await queue.ack(msgs[0].message_id) == 1
    assert await queue.pending_count() == 0
    assert await queue.read("worker-2", block_ms=100) == []


@pytest.mark.redis
@pytest.mark.asyncio
async def test_unacked_message_is_reclaimed_by_another_consumer(queue: TaskQueue) -> None:
    """kill -9 的核心语义：未 ack 的消息被**另一个** consumer 认领。

    worker-1 拿到消息后"崩溃"（不 ack）；等空闲时长过去后，worker-2 通过
    XAUTOCLAIM 接管同一消息 id。若这条不成立，崩溃的任务就永远卡在 PEL 里。
    """
    await queue.enqueue("org/app", 7, "sha-abc")
    first = await queue.read("worker-crashed", block_ms=100)
    assert len(first) == 1

    # 空闲时长设 0：XAUTOCLAIM 的 min-idle-time 以毫秒计，0 表示"任何已投递
    # 未 ack 的都算可认领"，等价于等待期已过，不必让测试真的睡 60 秒。
    reclaimed = await queue.reclaim_stale("worker-new", min_idle_ms=0)
    assert [m.message_id for m in reclaimed] == [first[0].message_id], (
        "崩溃 worker 的消息没有被新 worker 认领"
    )
    assert reclaimed[0].identity == ("org/app", 7, "sha-abc")


@pytest.mark.redis
@pytest.mark.asyncio
async def test_reclaim_skips_fresh_messages(queue: TaskQueue) -> None:
    """空闲时长未到的消息**不能**被抢走，否则等于放弃并发度。"""
    await queue.enqueue("org/app", 7, "sha-abc")
    await queue.read("worker-1", block_ms=100)
    assert await queue.reclaim_stale("worker-2", min_idle_ms=60_000) == []


@pytest.mark.redis
@pytest.mark.asyncio
async def test_duplicate_enqueue_yields_two_messages(queue: TaskQueue) -> None:
    """重复入队产生两条消息——这是允许的，幂等在消费端（条件 UPDATE 认领）。

    刻意断言"两条"而不是"一条"：如果哪天有人在 enqueue 里加去重，这条例外要能
    提醒他重新想清楚——入队去重会让"写成功但进程随即崩溃"的窗口丢消息，
    而消费端幂等已经足够。
    """
    await queue.enqueue("org/app", 7, "sha-abc")
    await queue.enqueue("org/app", 7, "sha-abc")
    assert await queue.depth() == 2
    msgs = await queue.read("worker-1", count=10, block_ms=100)
    assert len(msgs) == 2
    assert msgs[0].message_id != msgs[1].message_id
    assert msgs[0].identity == msgs[1].identity


@pytest.mark.redis
@pytest.mark.asyncio
async def test_read_returns_empty_list_not_none(queue: TaskQueue) -> None:
    """空队列时返回 []：调用点不必写 `or []`，也不会把 None 传给 for 循环。"""
    assert await queue.read("worker-1", block_ms=100) == []


def test_task_key_matches_webhook_route() -> None:
    """队列层与 webhook 层的任务键必须一致（两边刻意不互相 import）。

    一致性靠这个测试保证，而不是靠"记得同步"。不一致的后果很隐蔽：队列消息
    找不到对应任务行，worker 只能把它 ack 掉，任务静默消失。
    """
    from apps.code_review_pipeline.routing.github_review_route import (
        build_task_key as route_build_task_key,
    )
    from apps.code_review_pipeline.task_queue import build_task_key

    cases = [("org/app", 7, "deadbeefcafe"), ("a/b", 1, ""), ("o/r", 0, "abc")]
    for repo, pr, sha in cases:
        assert build_task_key(repo, pr, sha) == route_build_task_key(repo, pr, sha), (
            f"任务键在两处分叉: {(repo, pr, sha)}"
        )


def test_default_group_name_is_the_planned_one() -> None:
    assert CONSUMER_GROUP == "moa-workers"
