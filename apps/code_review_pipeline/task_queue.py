"""任务队列：Redis Streams（D3）。

为什么是 Streams 而不是 arq / Celery / Kafka：

- **arq** 是 asyncio 任务队列，语义是"跑一个 Python 协程"，它的重试与唯一性
  都得自己包一层；我们需要的恰恰是"至少一次投递 + 消费端幂等"，而 Streams 的
  PEL（Pending Entries List）**原生就是这个模型**：XREADGROUP 投递即进 PEL，
  XACK 才移出，worker 崩了消息留在 PEL 里等 XAUTOCLAIM 认领。
- **Celery** 适合"任务队列 + 结果后端"，但它把执行模型锁死在 broker 语义上，
  且需要额外的结果存储。
- **Kafka** 的分区与消费组更重，这个量级（单机 demo）用它属于过度工程。

**ack 时机：任务行状态已落库之后**（计划里的硬要求）。这条决定了崩溃语义：

    XREADGROUP -> 处理 -> 状态落库 -> XACK

崩在落库之前：消息留在 PEL，重启后 XAUTOCLAIM 重新认领，重跑。
崩在落库之后、ack 之前：消息被重新认领，但条件 UPDATE 的认领会失败（任务已非
queued），worker 直接 ack 跳过。**两层幂等叠加**，所以 ack 早一点也不会重复执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("moa.code_review.queue")

STREAM_KEY = "moa:tasks"
CONSUMER_GROUP = "moa-workers"


@dataclass(frozen=True)
class TaskMessage:
    """一条队列消息。``message_id`` 是 Stream 的 entry id，与任务身份无关。"""

    message_id: str
    repo: str
    pr_number: int
    head_sha: str
    task_key: str

    @property
    def identity(self) -> tuple[str, int, str]:
        """与 ``review_store`` 的任务方法对齐的身份三元组。"""
        return (self.repo, self.pr_number, self.head_sha)


def build_task_key(repo: str, pr_number: int, head_sha: str) -> str:
    """与 ``github_review_route.build_task_key`` 同源。

    这里**重新实现**而不是 import，是有意的隔离：webhook 路由不该被队列层
    依赖（反之亦然），否则将来任一侧重构任务键格式就会连带另一个。两边各自
    有一致性测试（test_task_key_parity）盯着真值，不靠"记得同步"。
    """
    return f"{repo}#{int(pr_number or 0)}@{head_sha or 'unknown'}"


class TaskQueue:
    def __init__(
        self,
        redis: Any,
        *,
        stream: str = STREAM_KEY,
        group: str = CONSUMER_GROUP,
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._group = group

    async def ensure_group(self) -> bool:
        """幂等建组。返回 True 表示这次真的新建了。"""
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
            return True
        except Exception as exc:  # BUSYGROUP
            if "BUSYGROUP" not in str(exc):
                raise
            return False

    async def enqueue(self, repo: str, pr_number: int, head_sha: str) -> str:
        """入队，返回 Stream entry id。

        **无脑重复入队是安全的**：消费端用条件 UPDATE 认领，同一任务无论被投递
        几次都只会真正执行一次（见模块 docstring 的两层幂等）。所以调用方不需要
        "先查再入队"——那种写法会在查与写之间留窗口，反而更容易漏。
        """
        task_key = build_task_key(repo, pr_number, head_sha)
        return await self._redis.xadd(
            self._stream,
            {
                "task_key": task_key,
                "repo": repo,
                "pr_number": str(int(pr_number or 0)),
                "head_sha": head_sha or "",
            },
        )

    def _decode(self, message_id: bytes | str, fields: dict[Any, Any]) -> TaskMessage:
        # 键**和值**都要解码。客户端是 decode_responses=False，两边都是 bytes；
        # 只解键的话 repo 会是 b"org/app"，拿它查任务行必然查不到 —— 而表现是
        # "worker 静默跳过"，不报错。
        f = {}
        for k, v in fields.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            f[key] = v.decode() if isinstance(v, bytes) else v
        return TaskMessage(
            message_id=message_id.decode() if isinstance(message_id, bytes) else str(message_id),
            repo=f.get("repo", ""),
            pr_number=int(f.get("pr_number") or 0),
            head_sha=f.get("head_sha", ""),
            task_key=f.get("task_key", ""),
        )

    async def read(
        self, consumer: str, *, count: int = 1, block_ms: int = 5000
    ) -> list[TaskMessage]:
        """XREADGROUP 拉新消息。空结果返回空列表（不是 None）。"""
        result = await self._redis.xreadgroup(
            self._group, consumer, {self._stream: ">"}, count=count, block=block_ms
        )
        out: list[TaskMessage] = []
        for _stream_name, entries in result or []:
            for message_id, fields in entries:
                out.append(self._decode(message_id, fields))
        return out

    async def reclaim_stale(
        self, consumer: str, *, min_idle_ms: int = 60_000, count: int = 10
    ) -> list[TaskMessage]:
        """XAUTOCLAIM 认领空闲超时的 PEL 消息（上一个 worker 崩掉的那些）。

        必须在每轮循环**开头**调用，而不是只在启动时：worker 会在运行中反复崩，
        只在启动时认领一次的话，第二轮崩溃的消息会一直躺在 PEL 里直到下次重启。

        redis-py 8 的返回是 ``[next_start_id, messages, deleted_ids]``——游标在
        **第一个**元素（不是最后一个）。按"entries 在前"的直觉写会 unpack 出一个
        整数，报 ``cannot unpack non-iterable int``。

        另外 XAUTOCLAIM 只回收**已经进过 PEL** 的消息：从未被 XREADGROUP 投递过
        的消息不在其中，min_idle_time 也不适用。

        **终止条件必须靠 next_id == "0-0"，不能靠 entries 为空**。实测（真 Redis）：
        min_idle_ms=0 时每一轮都返回同一条消息、且 next_id 恒为 "0-0"，所以
        "entries 非空就继续"会变成**死循环**——worker 的整轮循环就此卡住，
        而且没有任何异常。next_id 归零的含义是"这一遍扫到流末尾了"，
        下一轮该从头再来（那属于下一轮循环的事，不该在本次调用里做）。

        但循环必须**至少执行一次**：初值就是 "0-0"（意为"从头扫"），拿它直接当
        while 的条件会让整个函数一次都不跑、静默返回空列表——认领静默失效，
        比死循环更难发现。
        """
        start_id = "0-0"
        claimed: list[TaskMessage] = []
        seen: set[str] = set()
        while True:
            result = await self._redis.xautoclaim(
                self._stream, self._group, consumer, min_idle_ms, start_id, count=count
            )
            if result is None:
                break
            # 注意要**解码**：客户端是 decode_responses=False，Redis 回的是
            # b"0-0"，直接 str() 会得到字符串 "b'0-0'"——它长得像合法的 stream id，
            # 于是 Redis 报 "Invalid stream ID specified"（实测踩过）。
            raw_next = result[0]
            start_id = raw_next.decode() if isinstance(raw_next, bytes) else str(raw_next)
            entries = result[1]
            if not entries:
                break
            for message_id, fields in entries:
                msg = self._decode(message_id, fields)
                if msg.message_id in seen:
                    continue
                seen.add(msg.message_id)
                claimed.append(msg)
            if start_id == "0-0":
                # 本遍已扫到流末尾。再用 "0-0" 发起下一次调用会拿到**同样**的那批
                # 消息（min_idle_ms=0 时尤其明显），于是变成死循环。
                break
        return claimed

    async def ack(self, message_id: str) -> int:
        return int(await self._redis.xack(self._stream, self._group, message_id))

    async def pending_count(self) -> int:
        """PEL 长度。非零且没有 worker 在推进 = 有任务卡住了。"""
        info = await self._redis.xpending(self._stream, self._group)
        return int(info.get("pending", 0)) if isinstance(info, dict) else int(info)

    async def depth(self) -> int:
        return int(await self._redis.xlen(self._stream))

    async def dump(self, limit: int = 20) -> list[dict[str, Any]]:
        """调试用：读出前若干条。"""
        rows = await self._redis.xrange(self._stream, count=limit)
        out = []
        for message_id, fields in rows:
            f = {
                (k.decode() if isinstance(k, bytes) else str(k)): v for k, v in fields.items()
            }
            out.append({"id": str(message_id), **{k: str(v) for k, v in f.items()}})
        return out
