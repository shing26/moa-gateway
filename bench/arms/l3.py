"""L3：Planner + N 并发 worker + Critic（§5 四档表）。

与 L2 的差别只有并发拓扑
------------------------
planner 与 Critic 原样复用 L2；每个 wave 里的 worker 各自拿一条独立 LLM client，
避免 ``last_metrics`` 这个单值通道在并发下串账。worker 只返回自己的结果，不写
共享裁决状态；wave 结束后由一个 reducer 按下标合并，Critic 仍在唯一的汇聚点裁决。

预算怎么分
----------
每个 wave 先取全局剩余额度，再按本轮 worker 数均分。这样并发调用不会各自拿到一份
"满额预算"；某个 lane 花得少，剩余额度会留给后续 wave 与 Critic。代价是同一个 wave
里的 worker 先按平均份额竞争，结论以真实账本为准。
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agents.tools import ToolRegistry

from bench.agent.critic import Critique, ReviewPolicy
from bench.agent.execution import (
    SubtaskExecution,
    SubtaskOutcome,
    merge_execution,
    ordered_outcomes,
)
from bench.agent.expert import ExpertPool
from bench.agent.loop import LLMBackedAgent
from bench.agent.planner import Plan, Subtask
from bench.agent.types import (
    Budget,
    CodingAgent,
    Step,
    SubtaskUsage,
    Usage,
)
from bench.arms.l2 import (
    L2PlannerWorkerCritic,
    PlanPolicy,
    handoff_prompt,
    render_steps,
    summarize_steps,
)
from bench.arms.registry import Arm

WorkerLLMFactory = Callable[[int], Any]


class L3ConcurrentWorkers(L2PlannerWorkerCritic):
    """实现 ``CodingAgent`` 协议的 L3 档。"""

    def __init__(
        self,
        *,
        pool: ExpertPool,
        workspace: Path,
        policy: PlanPolicy | None = None,
        review_policy: ReviewPolicy | None = None,
        worker_llm_factory: WorkerLLMFactory | None = None,
    ) -> None:
        super().__init__(
            pool=pool,
            workspace=workspace,
            policy=policy,
            review_policy=review_policy,
        )
        self._worker_llm_factory = worker_llm_factory or (
            lambda index: copy.deepcopy(pool.llm)
        )

    def _rerun_targets(
        self,
        *,
        plan: Plan,
        critique: Critique,
    ) -> tuple[int, ...] | None:
        known = {subtask.index for subtask in plan.subtasks}
        targets = tuple(index for index in critique.targets if index in known)
        # 没给 targets 或全给了未知下标时退回整计划，避免静默什么都不做。
        return targets or None

    async def _run_subtasks(
        self,
        *,
        workspace: Path,
        goal: str,
        plan: Plan,
        budget: Budget,
        used_usage: Usage,
        used_turns: int,
        only: tuple[int, ...] | None,
        critique: Critique | None,
        existing: tuple[SubtaskOutcome, ...],
        existing_usage: tuple[SubtaskUsage, ...],
    ) -> SubtaskExecution:
        selected = _selected(plan=plan, only=only)
        baseline = SubtaskExecution(
            outcomes=existing,
            usage=Usage(),
            turns=0,
            steps=(),
            model="",
            status="completed",
            subtask_usage=existing_usage,
        )
        if not selected:
            return baseline

        stopped = budget.stop_status(usage=used_usage, steps_used=used_turns)
        if stopped is not None:
            return SubtaskExecution(
                outcomes=existing,
                usage=Usage(),
                turns=0,
                steps=(),
                model="",
                status=stopped,
                subtask_usage=existing_usage,
            )

        remaining = budget.remaining(usage=used_usage, steps_used=used_turns)
        lane_cost = remaining.max_cost_usd / len(selected)
        lane_steps = remaining.max_steps // len(selected)
        if lane_steps <= 0:
            return SubtaskExecution(
                outcomes=existing,
                usage=Usage(),
                turns=0,
                steps=(),
                model="",
                status="step_limit",
                subtask_usage=existing_usage,
            )

        prior_by_index = {item.index: item for item in existing}
        tasks = [
            self._run_worker(
                workspace=workspace,
                goal=goal,
                subtask=subtask,
                prior=tuple(
                    item
                    for item in ordered_outcomes(prior_by_index)
                    if item.index < subtask.index
                ),
                critique=critique,
                budget=Budget(max_cost_usd=lane_cost, max_steps=lane_steps),
            )
            for subtask in selected
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged = baseline
        for subtask, result in zip(selected, results, strict=True):
            if isinstance(result, BaseException):
                result = _failed_worker(subtask=subtask, error=str(result))
            merged = merge_execution(merged, result)
        return merged

    async def _run_worker(
        self,
        *,
        workspace: Path,
        goal: str,
        subtask: Subtask,
        prior: tuple[SubtaskOutcome, ...],
        critique: Critique | None,
        budget: Budget,
    ) -> SubtaskExecution:
        session = LLMBackedAgent(
            self._worker_llm_factory(subtask.index),
            self._pool.tools,
        ).session(workspace=workspace, goal=goal, budget=budget)
        session.add_user_message(
            handoff_prompt(
                goal=goal,
                subtask=subtask,
                prior=prior,
                critique=critique,
            )
        )
        status = await session.advance(max_turns=budget.max_steps)
        usage = SubtaskUsage(
            index=subtask.index,
            usage=session.usage,
            turns=session.turns,
            status=status,
            model=session.model,
        )
        if status != "completed":
            return SubtaskExecution(
                outcomes=(),
                usage=session.usage,
                turns=session.turns,
                steps=tuple(session.steps),
                model=session.model,
                status=status,
                subtask_usage=(usage,),
                error=session.error,
            )
        return SubtaskExecution(
            outcomes=(
                SubtaskOutcome(
                    index=subtask.index,
                    instruction=subtask.instruction,
                    output=summarize_steps(session.steps),
                    trajectory=render_steps(session.steps),
                ),
            ),
            usage=session.usage,
            turns=session.turns,
            steps=tuple(session.steps),
            model=session.model,
            status=status,
            subtask_usage=(usage,),
        )


def _selected(*, plan: Plan, only: tuple[int, ...] | None) -> list[Subtask]:
    if only is None:
        return list(plan.subtasks)
    wanted = set(only)
    return [subtask for subtask in plan.subtasks if subtask.index in wanted]


def _failed_worker(*, subtask: Subtask, error: str) -> SubtaskExecution:
    """把并发任务里漏出来的异常收成一条可审计的 error，而不是炸掉整个 run。"""
    step = Step(
        index=0,
        action="call_tool",
        tool="l3_worker",
        observation=error[:2_000],
        outcome="failed",
    )
    return SubtaskExecution(
        outcomes=(),
        usage=Usage(),
        turns=0,
        steps=(step,),
        model="",
        status="error",
        subtask_usage=(
            SubtaskUsage(
                index=subtask.index,
                usage=Usage(),
                turns=0,
                status="error",
            ),
        ),
        error=error,
    )


def build_l3(*, llm: Any, workspace: Path, tools: ToolRegistry) -> CodingAgent:
    pool = ExpertPool(llm=llm, tools=tools)
    return L3ConcurrentWorkers(
        pool=pool,
        workspace=workspace,
        worker_llm_factory=lambda index: copy.deepcopy(llm),
    )


L3 = Arm(
    name="L3",
    description="Planner + N concurrent workers + Critic：扇出执行、reducer 合并、按子任务重跑",
    build=build_l3,
)


__all__ = [
    "L3",
    "L3ConcurrentWorkers",
    "WorkerLLMFactory",
    "build_l3",
]
