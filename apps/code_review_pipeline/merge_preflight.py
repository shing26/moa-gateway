"""合并通道的启动自检（ADR-021）。

合并通道最坏的失败形状是**静默不生效**：进程都在跑、日志没有 error，只是卡片
永远不来、或者按钮点了说"已失效"。这类前提条件（worker 起了吗、卡片有收件人吗、
HITL 存储是不是跨进程共享）在运行期都不报错，所以要在启动时逐条查出来说清楚。

「启用合并通道必须有共享数据库」那条更硬，由 ``app/config.py`` 在启动时直接
raise（配置层面就能判定）；本模块负责其余**只有跑起来才知道**的条件。
"""

from __future__ import annotations

from typing import Any


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
    return problems


def merge_channel_readiness(
    *,
    enabled: bool,
    channel: str,
    feishu_configured: bool,
    session_store: Any,
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
    )


__all__ = [
    "hitl_store_is_shared",
    "merge_channel_problems",
    "merge_channel_readiness",
]
