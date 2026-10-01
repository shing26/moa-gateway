"""任务 worker：独立进程，消费 Redis Streams 里的 PR 审查任务（D3）。

为什么必须是**独立进程**而不是网关里的后台协程：任务要跑 5 个 agent、可能等人审批，
一次能占几分钟。放在网关进程里，一个卡住的任务会连带拖垮同进程的所有 HTTP 请求，
而"worker 崩了只影响任务"正是这套底座想证明的事之一。

崩溃语义（推导见 task_queue 模块）：

    XREADGROUP -> 条件 UPDATE 认领 -> 执行 -> 状态落库 -> XACK

- 崩在落库前：消息留在 PEL，下一轮 XAUTOCLAIM 重新认领，重跑。
- 崩在落库后、ack 前：消息被重新认领，但认领失败（任务已非 queued），跳过并 ack。
- 两层幂等叠加，所以 ack 早一点也不会重复执行，ack 晚一点也不会丢任务。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from typing import Awaitable, Callable

from apps.code_review_pipeline.storage.review_store import (
    ReviewStore,
    TaskNotFound,
    build_review_store,
)
from apps.code_review_pipeline.task_queue import TaskMessage, TaskQueue
from apps.code_review_pipeline.task_state import TaskState

logger = logging.getLogger("moa.worker")

Processor = Callable[[TaskMessage], Awaitable[str]]


def _consumer_name() -> str:
    """consumer 名带上主机名与 pid：排查 PEL 时要能认出是谁认领的。"""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"


class TaskWorker:
    def __init__(
        self,
        queue: TaskQueue,
        store: ReviewStore,
        processor: Processor,
        *,
        consumer: str | None = None,
        reclaim_min_idle_ms: int = 60_000,
    ) -> None:
        self._queue = queue
        self._store = store
        self._processor = processor
        self._consumer = consumer or _consumer_name()
        self._reclaim_min_idle_ms = reclaim_min_idle_ms

    @property
    def consumer(self) -> str:
        return self._consumer

    async def run_once(self, *, block_ms: int = 5000) -> int:
        """跑一轮：先回收崩溃遗留，再拉新消息。返回处理条数。

        回收放在**每轮开头**而不是只在启动时：worker 会在运行中反复崩，只在启动
        时回收一次的话，第二轮崩的消息会一直躺在 PEL 里直到下次重启。
        """
        await self._queue.ensure_group()
        handled = 0
        for msg in await self._queue.reclaim_stale(
            self._consumer, min_idle_ms=self._reclaim_min_idle_ms
        ):
            handled += await self._handle(msg, source="reclaimed")
        for msg in await self._queue.read(self._consumer, block_ms=block_ms):
            handled += await self._handle(msg, source="fresh")
        return handled

    async def _handle(self, msg: TaskMessage, *, source: str) -> int:
        """处理一条消息，**无论成功失败都 ack**。

        不 ack 就等于把消息留在 PEL 里被反复重投。失败的情形另有归属：任务行会被
        推到 failed，人能看到、能显式 requeue；而让一条坏消息在队列里无限循环，
        只会把整个 worker 卡死在一个坏任务上。
        """
        identity = msg.identity
        try:
            # 只有**回收**来的消息才允许接管 running 的任务；正常新消息必须从
            # queued 认领。否则一个卡住很久的 running 任务会被"新投递"意外复活，
            # 而 XAUTOCLAIM 路径本来就是为崩溃恢复准备的。
            claimed = self._store.acquire_task(identity, reclaim=(source == "reclaimed"))
        except TaskNotFound:
            logger.warning("task row missing for %s; acking to drop it", identity)
            await self._queue.ack(msg.message_id)
            return 1
        if not claimed:
            # 已被别的 worker 认领，或任务已是终态。这正是重复投递的正常归宿：
            # ack 掉，否则它会一直被重投而永远做不了事。
            logger.info("skipping %s: not claimable (%s)", identity, source)
            await self._queue.ack(msg.message_id)
            return 1

        try:
            outcome = await self._processor(msg)
        except Exception:
            logger.exception("task failed: %s", identity)
            self._mark_failed(identity)
            await self._queue.ack(msg.message_id)
            return 1

        self._apply_outcome(identity, outcome)
        # ack 放在状态落库**之后**——这是底座的核心契约。
        await self._queue.ack(msg.message_id)
        return 1

    def _mark_failed(self, identity: tuple[str, int, str]) -> None:
        try:
            self._store.transition_task(identity, "fail")
        except Exception:
            logger.exception("could not mark task failed: %s", identity)

    def _apply_outcome(self, identity: tuple[str, int, str], outcome: str) -> None:
        """把 processor 的返回值映射成状态动作。

        processor 只返回三种结果：需要审批 / 完成 / 失败。映射集中在这里，
        processor 不碰状态机词汇——它只该关心"审出了什么"。
        """
        action = {
            TaskState.WAITING_APPROVAL.value: "request_approval",
            TaskState.DONE.value: "complete",
            TaskState.FAILED.value: "fail",
        }.get(outcome, "request_approval")
        try:
            self._store.transition_task(identity, action)
        except Exception:
            logger.exception("could not persist outcome %s for %s", outcome, identity)

    async def run_forever(self, *, interval_s: float = 1.0) -> None:
        while True:
            try:
                handled = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker loop iteration failed")
                handled = 0
            if not handled:
                await asyncio.sleep(interval_s)


async def review_processor(msg: TaskMessage) -> str:
    """跑真实的 5-agent 审查流水线。

    **队列消息只带身份，PR 数据现取**——不把整份 webhook payload 存进任务行。
    两个理由：payload 很大（每个文件带 patch），任务行会迅速膨胀；而且存下来的
    payload 是**投递那一刻**的快照，等 worker 真正跑到时 PR 可能又推了新 commit，
    用快照审会审一个已经不存在的版本。现取拿到的是当下真实的 diff。

    action 固定填 "synchronize"：build_pr_context_from_github 只认
    opened/synchronize/reopened，而这里的语义正是"这份代码要审一遍"。真正的
    action 判断早在 webhook 入口做过了，worker 不需要重做。
    """
    from app.models.events import MoAEvent
    from apps.code_review_pipeline.agents.code_review_pipeline import CodeReviewPipeline
    from apps.code_review_pipeline.storage.review_store import ReviewRecord

    payload = {
        "action": "synchronize",
        "repository": {"full_name": msg.repo},
        "pull_request": {"number": msg.pr_number, "head": {"sha": msg.head_sha}},
    }
    event = MoAEvent(
        trace_id=msg.task_key,
        event=None,
        session_id=msg.repo,
        text="",
        context=payload,
    )
    pipeline = CodeReviewPipeline.from_env()
    pr, result = await pipeline.run(event)

    # 审查结论落进任务行：findings 数与"是否需要人工"是 D4 审批页与写回判据的
    # 数据来源。此处失败不该让整个任务失败（结论只是附加信息），故吞掉异常。
    try:
        attrs = ("triage", "static_analysis", "semantic_review", "test_coverage")
        total = 0
        for attr in attrs:
            section = getattr(result, attr, None)
            total += len(getattr(section, "findings", ()) or ())
        store = build_review_store()
        store.save(
            ReviewRecord(
                trace_id=result.trace_id,
                repo=pr.repo,
                pr_number=pr.pr_number,
                head_sha=pr.head_sha,
                author=pr.author,
                findings_count=total,
                need_human_review=result.overall_need_human_review,
                raw={
                    "base_sha": pr.base_sha,
                    "title": pr.title,
                    "html_url": pr.html_url,
                    "diff_url": pr.diff_url,
                },
            )
        )
    except Exception:
        logger.warning("could not persist review findings for %s", msg.task_key)

    return TaskState.WAITING_APPROVAL.value


def build_worker() -> TaskWorker:
    """装配 worker。Redis 与 store 各自独立连接：队列挂了不该影响读库。"""
    from redis.asyncio import Redis

    from app.config import settings

    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    return TaskWorker(TaskQueue(redis), build_review_store(), review_processor)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # Windows 下 psycopg 拒绝 ProactorEventLoop（与 app/__main__.py 同一处理）。
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    worker = build_worker()
    logger.info("worker starting as %s", worker.consumer)
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        logger.info("worker interrupted; shutting down")


if __name__ == "__main__":
    main()
