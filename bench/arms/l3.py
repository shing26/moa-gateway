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
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.agents.tools import ToolRegistry
from app.orchestration.workspace_isolation import (
    collect_workspace_write_set,
    create_isolated_workspace,
    merge_workspace_write_sets,
    summarize_write_sets,
)

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


def clone_llm_factory(llm: Any) -> WorkerLLMFactory:
    """默认的 worker 客户端工厂：每条 lane 深拷贝一份独立 client。

    ``last_metrics`` 是 client 上的单值通道，多个 worker 共用一条会被后写覆盖，并发
    下就串账。拷贝一份让每条 lane 自己记自己的账。工厂在这里**只定义一次**，``__init__``
    与 ``build_l3`` 都复用它，改拷贝策略只有一处。
    """
    return lambda index: copy.deepcopy(llm)


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
        self._worker_llm_factory = worker_llm_factory or clone_llm_factory(pool.llm)
        self._workspace_wave = 0
        self._workspace_writes: list[dict[str, Any]] = []
        self._workspace_conflicts: list[dict[str, Any]] = []
        self._workspace_merges: list[dict[str, Any]] = []

    async def run(
        self,
        *,
        workspace: Path,
        goal: str,
        budget: Budget,
    ):
        """跑一轮 L3，并把并行写证据挂到同一个 ``AgentRun`` 出口。"""
        self._workspace_wave = 0
        self._workspace_writes = []
        self._workspace_conflicts = []
        self._workspace_merges = []
        run = await super().run(workspace=workspace, goal=goal, budget=budget)
        return replace(
            run,
            workspace_writes=tuple(self._workspace_writes),
            workspace_conflicts=tuple(self._workspace_conflicts),
            workspace_merges=tuple(self._workspace_merges),
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
        rerun_indices: tuple[int, ...] | None,
        critique: Critique | None,
        existing: tuple[SubtaskOutcome, ...],
        existing_usage: tuple[SubtaskUsage, ...],
    ) -> SubtaskExecution:
        selected = _selected(plan=plan, rerun_indices=rerun_indices)
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

        merged = baseline
        pending = {subtask.index: subtask for subtask in selected}
        completed = {item.index for item in existing}
        while pending:
            ready = [
                subtask
                for index, subtask in sorted(pending.items())
                if all(dep in completed for dep in subtask.depends_on)
            ]
            if not ready:
                blockers = {
                    index: list(subtask.depends_on)
                    for index, subtask in sorted(pending.items())
                }
                return replace(
                    merged,
                    status="error",
                    error=f"L3 依赖无法满足，剩余子任务: {blockers}",
                )

            stopped = budget.stop_status(
                usage=used_usage + merged.usage,
                steps_used=used_turns + merged.turns,
            )
            if stopped is not None:
                return replace(merged, status=stopped)

            remaining = budget.remaining(
                usage=used_usage + merged.usage,
                steps_used=used_turns + merged.turns,
            )
            lane_cost = remaining.max_cost_usd / len(ready)
            lane_steps = remaining.max_steps // len(ready)
            if lane_steps <= 0:
                return replace(merged, status="step_limit")

            self._workspace_wave += 1
            prior_by_index = {
                item.index: item for item in (*existing, *merged.outcomes)
            }
            tasks = [
                self._run_worker_isolated(
                    workspace=workspace,
                    goal=goal,
                    subtask=subtask,
                    prior=tuple(
                        item
                        for item in ordered_outcomes(prior_by_index)
                        if item.index < subtask.index
                    ),
                    critique=critique,
                    budget=Budget(
                        max_cost_usd=lane_cost,
                        max_steps=lane_steps,
                        # token 兜底不按 lane 均分——它是防止单条 lane 无节制刷 token
                        # 的全局闸门（§5.3），与主线按美元均分是两件事。
                        max_tokens=remaining.max_tokens,
                    ),
                    round_index=self._workspace_wave,
                )
                for subtask in ready
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            write_sets: list[dict[str, Any]] = []
            for subtask, result in zip(ready, results, strict=True):
                pending.pop(subtask.index, None)
                if isinstance(result, Exception):
                    execution = _failed_worker(subtask=subtask, error=str(result))
                    write_set = None
                elif isinstance(result, BaseException):
                    # 取消/`KeyboardInterrupt`/`SystemExit` 不是"某个 worker 出错"，
                    # 而是这次 run 整体被叫停——ADR-010 决策 4 要求原样上抛。
                    raise result
                else:
                    execution, write_set = result
                merged = merge_execution(merged, execution)
                if write_set is not None:
                    write_sets.append(write_set)
                if execution.status == "completed":
                    completed.add(subtask.index)

            if write_sets:
                merge_result = await asyncio.to_thread(
                    merge_workspace_write_sets,
                    workspace,
                    write_sets,
                )
                self._workspace_writes.extend(summarize_write_sets(write_sets))
                self._workspace_merges.extend(merge_result.applied)
                self._workspace_conflicts.extend(merge_result.conflicts)

            if merged.status != "completed":
                return merged
        return merged

    async def _run_worker_isolated(
        self,
        *,
        workspace: Path,
        goal: str,
        subtask: Subtask,
        prior: tuple[SubtaskOutcome, ...],
        critique: Critique | None,
        budget: Budget,
        round_index: int,
    ) -> tuple[SubtaskExecution, dict[str, Any] | None]:
        isolated = None
        try:
            isolated = await asyncio.to_thread(create_isolated_workspace, workspace)
            worker_tools = self._pool.tools.for_workspace(isolated.root)
            execution = await self._run_worker(
                workspace=isolated.root,
                goal=goal,
                subtask=subtask,
                prior=prior,
                critique=critique,
                budget=budget,
                tools=worker_tools,
            )
            write_set = await asyncio.to_thread(
                collect_workspace_write_set,
                source=workspace,
                isolated_root=isolated.root,
                writer_id=f"round-{round_index}/subtask-{subtask.index}",
                task=subtask.instruction,
                round_index=round_index,
                accepted=execution.status == "completed",
            )
            return execution, write_set
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _failed_worker(subtask=subtask, error=str(exc)), None
        finally:
            if isolated is not None:
                await asyncio.to_thread(isolated.cleanup)

    async def _run_worker(
        self,
        *,
        workspace: Path,
        goal: str,
        subtask: Subtask,
        prior: tuple[SubtaskOutcome, ...],
        critique: Critique | None,
        budget: Budget,
        tools: ToolRegistry | None = None,
    ) -> SubtaskExecution:
        session = LLMBackedAgent(
            self._worker_llm_factory(subtask.index),
            tools or self._pool.tools,
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


def _selected(
    *, plan: Plan, rerun_indices: tuple[int, ...] | None
) -> list[Subtask]:
    if rerun_indices is None:
        return list(plan.subtasks)
    wanted = set(rerun_indices)
    return [subtask for subtask in plan.subtasks if subtask.index in wanted]


def _failed_worker(*, subtask: Subtask, error: str) -> SubtaskExecution:
    """把并发任务里漏出来的异常收成一条可审计的 error，而不是炸掉整个 run。

    刻意**不**造一条假的 ``Step``：`l3_worker` 不是注册表里的工具，写进轨迹等于宣称
    发生过一次并不存在的工具调用。与 L2 的 worker 出错一致——错误经
    ``SubtaskExecution.error`` 上浮到 ``AgentRun.error``，轨迹里不留伪证据。
    """
    return SubtaskExecution(
        outcomes=(),
        usage=Usage(),
        turns=0,
        steps=(),
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
    # 不显式传 worker_llm_factory：默认就是 clone_llm_factory(pool.llm)，工厂策略
    # 只在 __init__ 里定一处。
    return L3ConcurrentWorkers(pool=pool, workspace=workspace)


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
    "clone_llm_factory",
]
