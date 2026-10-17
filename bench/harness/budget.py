"""两档预算策略（§5.3）：等预算主实验（B1）与放宽复跑（B3）。

B1 回答的是"同等花费下谁更强"，所以**四档共用同一个美元上限**——档位之间唯一的
变量必须是编排拓扑，不是预算。B3 只对 B1 里**失败**的档位放宽上限再跑一遍，回答
"协作能不能做到单体无论如何做不到的事"，并如实记下它多花了多少（
``RunResult.incremental_cost_usd``）。

token 上限是第二条兜底（防止单个 agent 无节制地刷 token），两档共用同一个值，且
**不做主线判据**——主线判美元。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from bench.agent.types import Budget, RunResult

#: 预算档位。``B1`` = 等预算主实验；``B3`` = 只对 B1 失败档位放宽的复跑。
BudgetTier = Literal["B1", "B3"]

B1_TIER: BudgetTier = "B1"
B3_TIER: BudgetTier = "B3"

#: 哪些判决算"B1 失败、值得按 B3 复跑"。``pass`` 不用复跑；``error`` 是 API/超时
#: 一类的噪音，放宽预算答不了能力问题，排除掉才不污染 B3。
_B3_TRIGGERS = frozenset({"fail", "budget_exhausted"})


@dataclass(frozen=True)
class BudgetPolicy:
    """两档预算的单一来源。

    ``relaxed_*`` 默认取主实验的三倍——"放宽"要宽到足以区分"做不到"与"预算不够"，
    具体数值由真实 run 反推（§12 把数值列为待定）。
    """

    main_cost_usd: float = 1.5
    main_steps: int = 30
    relaxed_cost_usd: float = 4.5
    relaxed_steps: int = 90
    #: token 兜底上限；0 表示不设。两档共用。
    max_tokens: int = 0

    def b1(self) -> Budget:
        """主实验预算：四档共用这一个。"""
        return Budget(
            max_cost_usd=self.main_cost_usd,
            max_steps=self.main_steps,
            max_tokens=self.max_tokens,
        )

    def b3(self) -> Budget:
        """放宽复跑预算：只给 B1 失败的档位。"""
        return Budget(
            max_cost_usd=self.relaxed_cost_usd,
            max_steps=self.relaxed_steps,
            max_tokens=self.max_tokens,
        )

    def for_tier(self, tier: str) -> Budget:
        if tier == B3_TIER:
            return self.b3()
        if tier == B1_TIER:
            return self.b1()
        raise ValueError(f"未知预算档位: {tier!r}（只认 {B1_TIER} / {B3_TIER}）")


def b3_candidates(results: Iterable[RunResult]) -> tuple[tuple[str, str], ...]:
    """从 B1 结果里挑出需要 B3 放宽复跑的 ``(task_id, arm)``。

    只看 ``budget_tier == "B1"`` 的 run，取 ``fail``（没做成）与 ``budget_exhausted``
    （太贵）两类；``pass`` 不必复跑，``error`` 是噪音。同一个 ``(task, arm)`` 出现
    多次只留一次，保持首次出现的顺序。
    """
    seen: dict[tuple[str, str], None] = {}
    for result in results:
        if result.budget_tier != B1_TIER:
            continue
        if result.outcome not in _B3_TRIGGERS:
            continue
        seen.setdefault((result.task_id, result.arm), None)
    return tuple(seen)


__all__ = [
    "B1_TIER",
    "B3_TIER",
    "BudgetPolicy",
    "BudgetTier",
    "b3_candidates",
]
