"""两档预算策略（§5.3）：等预算主实验（B1）与放宽复跑（B3）。

B1 回答的是"同等花费下谁更强"，所以**四档共用同一个美元上限**——档位之间唯一的
变量必须是编排拓扑，不是预算。B3 只对 B1 里**失败**的档位放宽上限再跑一遍，回答
"协作能不能做到单体无论如何做不到的事"，并如实记下它多花了多少（
``RunResult.incremental_cost_usd``）。

token 上限默认是第二条兜底（防止单个 agent 无节制地刷 token），两档共用同一个值，
**不做主线判据**——主线判美元。

**降级路径：主轴为 token。** 当 provider 不给成本信号（如免费端点）时 ``cost_usd``
恒为 0，"同等花费谁更强"无从算起，美元这条判据会永远不触发。此时把 ``axis`` 切成
``tokens``：四档共用同一个 **token** 上限，美元这条不设，token 成为唯一可比的货币。
两个主轴是互斥的，不会同时生效。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from bench.agent.types import (
    B1_TIER,
    B3_TIER,
    COST_AXIS,
    TOKEN_AXIS,
    Budget,
    BudgetAxis,
    BudgetTier,
    RunResult,
)

#: 哪些判决算"B1 失败、值得按 B3 复跑"。``pass`` 不用复跑；``error`` 是 API/超时
#: 一类的噪音，放宽预算答不了能力问题，排除掉才不污染 B3。
_B3_TRIGGERS = frozenset({"fail", "budget_exhausted"})

#: 主轴为 token 时不设美元上限用的哨兵。``Budget.stop_status`` 判
#: ``usage.cost_usd >= max_cost_usd``——取 inf 让美元这条永不触发，判停交给 token。
NO_COST_LIMIT = float("inf")


@dataclass(frozen=True)
class BudgetPolicy:
    """两档预算的单一来源。

    ``axis`` 决定"等预算"比什么（§5.3）：``cost`` 是默认的正常路径（有定价的模型），
    ``tokens`` 是 provider 不给成本信号时的降级路径。

    ``relaxed_*`` 默认取主实验的三倍——"放宽"要宽到足以区分"做不到"与"预算不够"，
    具体数值由真实 run 反推（§12 把数值列为待定）。
    """

    #: 等预算主轴：``cost`` = 共用美元上限；``tokens`` = 共用 token 上限。
    axis: BudgetAxis = COST_AXIS
    #: axis == "cost" 时的两档美元上限。
    main_cost_usd: float = 1.5
    relaxed_cost_usd: float = 4.5
    #: 两档的步数兜底。
    main_steps: int = 30
    relaxed_steps: int = 90
    #: axis == "tokens" 时的两档 token 上限（主轴）。
    main_tokens: int = 0
    relaxed_tokens: int = 0
    #: axis == "cost" 时的 token 兜底上限；0 表示不设。两档共用。
    max_tokens: int = 0

    def b1(self) -> Budget:
        """主实验预算：四档共用这一个。"""
        return self._tier_budget(main=True)

    def b3(self) -> Budget:
        """放宽复跑预算：只给 B1 失败的档位。"""
        return self._tier_budget(main=False)

    def for_tier(self, tier: str) -> Budget:
        if tier == B3_TIER:
            return self.b3()
        if tier == B1_TIER:
            return self.b1()
        raise ValueError(f"未知预算档位: {tier!r}（只认 {B1_TIER} / {B3_TIER}）")

    def _tier_budget(self, *, main: bool) -> Budget:
        label = "主实验" if main else "放宽复跑"
        steps = self.main_steps if main else self.relaxed_steps
        if self.axis == TOKEN_AXIS:
            tokens = self.main_tokens if main else self.relaxed_tokens
            if tokens <= 0:
                raise ValueError(
                    f"预算主轴是 tokens，但{label}的 token 上限未设（需 > 0）"
                )
            # 美元这条留空：免费端点的 cost_usd 恒为 0，不能让它成为判据。
            return Budget(
                max_cost_usd=NO_COST_LIMIT, max_steps=steps, max_tokens=tokens
            )
        if self.axis != COST_AXIS:
            raise ValueError(
                f"未知预算主轴: {self.axis!r}（只认 {COST_AXIS} / {TOKEN_AXIS}）"
            )
        cost = self.main_cost_usd if main else self.relaxed_cost_usd
        return Budget(max_cost_usd=cost, max_steps=steps, max_tokens=self.max_tokens)


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


@dataclass(frozen=True)
class B3Run:
    """一次 B3 复跑的全部参数——由 B1 结果推导，不让调用方手填。"""

    task_id: str
    arm: str
    budget: Budget
    #: 同 (task, arm) 的 B1 花费；增量成本 = 本次 - 它。
    baseline_cost_usd: float
    #: 同 (task, arm) 的 B1 token 用量；主轴是 token 时增量按它算。
    baseline_tokens: int = 0


def plan_b3_reruns(
    results: Iterable[RunResult],
    *,
    policy: BudgetPolicy | None = None,
) -> tuple[B3Run, ...]:
    """把 B1 结果转成 B3 复跑计划（§5.3 的执行口径）。

    ``b3_candidates`` 只回答"该复跑谁"，这里把**基线花费**一并带出来——增量成本因此
    是推出来的，不是调用方手填一个默认 0。只对 B1 里 ``fail`` / ``budget_exhausted``
    的档位生成计划；``b3_candidates`` 已经保证这一点。
    """
    policy = policy or BudgetPolicy()
    budget = policy.b3()

    # 每个候选的基线 = 它在 B1 上所有尝试的总花费（多次取和，与报告口径一致）。
    # 美元与 token 各存一份：主轴切到 tokens 时增量仍要有可比的分母。
    cost_baselines: dict[tuple[str, str], float] = {}
    token_baselines: dict[tuple[str, str], int] = {}
    for result in results:
        if result.budget_tier != B1_TIER:
            continue
        if result.outcome not in _B3_TRIGGERS:
            continue
        key = (result.task_id, result.arm)
        usage = result.agent_run.usage
        cost_baselines[key] = cost_baselines.get(key, 0.0) + usage.cost_usd
        token_baselines[key] = token_baselines.get(key, 0) + usage.total_tokens

    return tuple(
        B3Run(
            task_id=task_id,
            arm=arm,
            budget=budget,
            baseline_cost_usd=cost,
            baseline_tokens=token_baselines.get((task_id, arm), 0),
        )
        for (task_id, arm), cost in cost_baselines.items()
    )


__all__ = [
    "B1_TIER",
    "B3_TIER",
    "B3Run",
    "BudgetPolicy",
    "BudgetTier",
    "NO_COST_LIMIT",
    "b3_candidates",
    "plan_b3_reruns",
]
