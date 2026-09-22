"""执行期重试：把 agent 抛出的异常变成"带归因的一次重试"，再失败就交给人工。

为什么是模块级纯函数
--------------------
两条引擎（``MoAPipeline`` 与 ``LangGraphOrchestrator``）必须共用同一套重试语义，
否则 ADR-008 的等价性会在失败场景上分叉。它无状态，所以做成纯函数而不是注入的
协作者：不必改 ``__init__``，也就不必同步 ``test_langgraph_adapter.py`` 里那条
"协作者表面必须等于 MODELLED ∪ OUT_OF_SCOPE" 的漂移守卫。

重试预算由状态机结构决定
------------------------
``RETRY_BUDGET`` 与 ``app/fsm/state_machine.py`` 的表是同一件事的两面：表里只允许
一跳 ``EXECUTING --TASK_FAILED--> RETRY --TASK_FAILED--> SUSPENDED``，所以预算恒为 1。
它不是配置项——改预算就是改表。``tests/unit/test_agent_retry.py`` 有漂移守卫钉住两者。

工具级失败不归这里管
--------------------
ReAct 循环把工具异常转成 observation 让模型下一轮自愈（``app/agent_core/react.py``），
那是更细粒度的恢复，且盲目重试有副作用的工具（写笔记、发消息）本身是危险的。
本模块只处理**上抛到 pipeline 的基础设施异常**：LLM 超时、网络、解析失败。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from app.agents.contract import AgentEnvelope

logger = logging.getLogger("moa.agents.retry")

# 重试次数（不含首次尝试）。与状态机表的 RETRY 跳数一致，见模块 docstring。
RETRY_BUDGET = 1
# 重试前的固定退避。用 asyncio.sleep，不阻塞事件循环。
RETRY_BACKOFF_MS = 200

_METRICS_KEY = "llm_metrics"


class AgentExecutionFailed(Exception):
    """重试预算耗尽。

    携带尝试次数与最后一次原因：pipeline 据此决定"升级人工"时要在卡片与审计里
    写清试了几次、为什么失败，而不是只剩一句 ``agent execution failed``。
    """

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"{type(last_error).__name__}: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


def _absorb_metrics(slot: dict[str, object], total: dict[str, Any]) -> dict[str, Any]:
    """把本轮尝试写下的 ``llm_metrics`` 并入累计值。

    失败尝试的 token/成本同样真实发生，不能因为"这一轮没成功"就丢掉——否则
    重试会让审计里的成本系统性偏低。数值字段累加，字符串字段取最后一次非空值。
    """
    attempt = slot.pop(_METRICS_KEY, None)
    if not isinstance(attempt, dict):
        return total
    for key, value in attempt.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if value:
                total[key] = value
            continue
        total[key] = total.get(key, 0) + value
    return total


async def execute_with_retry(
    agent: Any,
    envelope: AgentEnvelope,
    *,
    on_failure: Callable[[int, BaseException], Awaitable[None]] | None = None,
    budget: int = RETRY_BUDGET,
    backoff_ms: int = RETRY_BACKOFF_MS,
) -> tuple[str, int, str]:
    """执行 ``agent.execute``，失败则带归因重试，预算耗尽抛 ``AgentExecutionFailed``。

    返回 ``(output, attempts, "")``；``attempts`` 是实际尝试次数（成功那次也算）。
    每次失败都先调 ``on_failure(attempt, exc)``，调用方据此把 FSM 推进到 RETRY；
    最后一次失败时状态机表自己会把 RETRY 推到 SUSPENDED（表即预算），所以这里
    不需要"下一次尝试开始"的回调——重试期间状态就停在 RETRY。

    ``AgentEnvelope`` 是 frozen 的，所以归因通过 ``dataclasses.replace`` 生成新的
    envelope 传给下一次尝试（``agent_local_slot`` 仍是同一个可变 dict，跨尝试共享）。
    """
    slot = envelope.agent_local_slot
    total: dict[str, Any] = {}
    attempt = 0
    while True:
        attempt += 1
        try:
            output = await agent.execute(envelope)
        except asyncio.CancelledError:
            # 取消不是失败：重试会吞掉取消信号，必须原样上抛。
            raise
        except Exception as exc:  # noqa: BLE001 - 任何基础设施异常都可重试
            total = _absorb_metrics(slot, total)
            if on_failure is not None:
                await on_failure(attempt, exc)
            if attempt > budget:
                slot[_METRICS_KEY] = total
                raise AgentExecutionFailed(attempt, exc) from exc
            logger.warning(
                "agent attempt %d failed (%s: %s); retrying",
                attempt, type(exc).__name__, exc,
            )
            if backoff_ms > 0:
                await asyncio.sleep(backoff_ms / 1000)
            envelope = replace(
                envelope,
                retry_attempt=attempt,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
            continue
        total = _absorb_metrics(slot, total)
        slot[_METRICS_KEY] = total
        return output, attempt, ""


__all__ = [
    "AgentExecutionFailed",
    "RETRY_BACKOFF_MS",
    "RETRY_BUDGET",
    "execute_with_retry",
]
