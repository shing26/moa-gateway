"""合并通道的启动自检（ADR-021）。

合并通道最坏的失败形状是**静默不生效**：进程都在跑、日志没有 error，只是卡片
永远不来、或者按钮点了说"已失效"。这类前提条件（worker 起了吗、卡片有收件人吗、
HITL 存储是不是跨进程共享）在运行期都不报错，所以要在启动时逐条查出来说清楚。

「启用合并通道必须有共享数据库」那条更硬，由 ``app/config.py`` 在启动时直接
raise（配置层面就能判定）；本模块负责其余**只有跑起来才知道**的条件。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.config import settings

# worker 每轮轮询后写入；/healthz 与启动自检都读同一个键。
MERGE_WORKER_HEARTBEAT_KEY = "moa:merge_worker:heartbeat"


async def read_worker_heartbeat_age_s(
    *,
    redis_url: str | None = None,
    client: Any | None = None,
    now: datetime | None = None,
) -> float | None:
    """返回 merge worker 最后心跳距今的秒数；缺失或读失败返回 ``None``。

    **绝不抛异常**：它同时被 ``/healthz``（一个附加能力不该把健康检查打成 5xx）
    和 lifespan 启动自检（更不该让网关起不来）调用。任何读取、解码、时间解析
    失败都等价于"没读到心跳"，由调用方决定报成 degraded 还是启动告警。

    ``client`` 用于测试注入；传入时由调用方负责关闭，本函数不碰它的生命周期。
    """
    owns_client = client is None
    try:
        if client is None:
            from redis.asyncio import Redis

            client = Redis.from_url(
                redis_url or settings.redis_url,
                socket_timeout=5.0,
                decode_responses=True,
                protocol=2,
            )
        raw = await client.get(MERGE_WORKER_HEARTBEAT_KEY)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        at = datetime.fromisoformat(str(payload["at"]).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        # 时钟轻微回拨时 negative age 会被误读成"比刚写还新鲜"；按 0 处理足够。
        return max(0.0, (reference - at).total_seconds())
    except Exception:  # noqa: BLE001 - 心跳读取是附加探测：任意异常都降级为"没读到"
        return None
    finally:
        if owns_client and client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - 关闭探测客户端失败不影响探测结论
                pass


def worker_heartbeat_max_age_s(interval_s: float | None = None) -> float:
    """心跳超过这个年龄就算过期：``3 × 轮询间隔``。"""
    if interval_s is None:
        interval_s = float(settings.code_review_merge_poll_interval_s)
    return 3.0 * float(interval_s)


def hitl_store_is_shared(session_store: Any) -> bool:
    """HITL 待批记录是否真的跨进程共享（而不是退化成进程内存）。

    合并审批的卡片由 merge worker 进程登记、由网关进程认领。两边只有落在同一个
    Redis 上才认得到；Redis 不可达时 ``RedisHitlStorage`` 会静默降级为进程内存，
    于是点击落到"已失效"。

    ``RedisHitlStorage`` 的连接与降级判定发生在**第一次操作时**，不主动探测就只
    读到初值 False，所以这里必须真的读一次 ``__preflight__`` 这个不存在的键。

    探测本身**绝不允许抛异常**：``REDIS_ENABLE_FALLBACK=0`` 且 Redis 不可达时，
    底层会直接抛 ``ConnectionError``。它在 lifespan 里被调用，冒上去就是整个网关
    （包括与合并无关的聊天主路径）起不来——一个附加能力不该有这个权力。探测失败
    等价于"没共享"，交给调用方报出来。
    """
    storage = getattr(session_store, "_storage", None)
    if storage is None:
        return False
    try:
        storage.get(storage.key("__preflight__"))
    except Exception:  # noqa: BLE001 - 自检必须无副作用、绝不阻断启动
        return False
    return not bool(getattr(storage, "using_memory", False))


def merge_channel_problems(
    *,
    channel: str,
    feishu_configured: bool,
    hitl_shared: bool,
    worker_heartbeat_age_s: float | None = None,
) -> list[str]:
    """返回合并通道的前置条件问题清单；空列表 = 齐备。

    一次列全，而不是让人改一条重启一次再发现下一条。
    """
    problems: list[str] = []
    if not channel:
        problems.append(
            "未配置 CODE_REVIEW_MERGE_CHANNEL：CI 绿之后审批卡片没有收件人，"
            "通道会停在 awaiting_approval 而没人知道"
        )
    if not feishu_configured:
        problems.append(
            "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET：发不出审批卡片，"
            "合并审批永远到不了人面前"
        )
    if not hitl_shared:
        problems.append(
            "HITL 存储回退到了进程内存（Redis 不可达）：merge worker 登记的待批记录"
            "网关进程认领不到，人点了「批准」只会得到「该审批请求已失效」"
        )
    max_age = worker_heartbeat_max_age_s()
    if worker_heartbeat_age_s is None:
        problems.append(
            "merge worker 心跳缺失：合并通道已启用，但独立 worker 进程没有在跑，"
            "CI 绿之后不会有审批卡片。请启动 `python -m app.merge_worker`"
        )
    elif worker_heartbeat_age_s > max_age:
        problems.append(
            f"merge worker 心跳已过期（{worker_heartbeat_age_s:.0f}s > {max_age:.0f}s）："
            "worker 可能已停止或卡住，审批卡片不会推进。"
            "请确认 `python -m app.merge_worker` 在运行"
        )
    return problems


def merge_channel_readiness(
    *,
    enabled: bool,
    channel: str,
    feishu_configured: bool,
    session_store: Any,
    worker_heartbeat_age_s: float | None = None,
) -> list[str]:
    """启动时调用：把开关、静态配置与运行期探测合成一份问题清单。

    通道关闭时返回空列表，默认部署的启动日志因此保持干净。
    """
    if not enabled:
        return []
    return merge_channel_problems(
        channel=channel,
        feishu_configured=feishu_configured,
        hitl_shared=hitl_store_is_shared(session_store),
        worker_heartbeat_age_s=worker_heartbeat_age_s,
    )


__all__ = [
    "MERGE_WORKER_HEARTBEAT_KEY",
    "hitl_store_is_shared",
    "merge_channel_problems",
    "merge_channel_readiness",
    "read_worker_heartbeat_age_s",
    "worker_heartbeat_max_age_s",
]
