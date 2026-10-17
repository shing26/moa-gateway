"""L2：Planner + 1 worker + Critic（§5 四档表）。

与 L0/L1 的差别只在拓扑
-----------------------
循环、工具、契约、停止条件、账本形状全部沿用 ``LoopSession``（§5.0）。L2 多出来的
只有三段编排：**分解** → **逐子任务交接** → **评审**。这里不新造执行体——Critic 复用
L1 的 ``review`` 专家与 ``parse_verdict``，worker 是与 L0 相同的中性单 agent。

handoff 才是这一档的课题
-----------------------
ticket 的原话是"上下文怎么从规划者传到执行者"。所以每个子任务**开一条新的
``LoopSession``**：worker 拿到的只有子任务的 ``instruction`` 加 planner 附的
``context``，外加前面子任务已经产出的结果。它翻不到 planner 的脑子，也不必回头问
——这就是"带够上下文"的可检验含义。

把两个子任务塞进同一条会话会让上下文"顺手"带过去，恰好测不出 handoff；分开开会话
才是这一档要证明的东西。代价也要一起付：worker 的历史不跨子任务，那正是 L3 之前要
单独看清的一课（§5.2 讨论的是并发写收敛，不是历史复用）。

协调开销照记
------------
planner 的调用、每个子任务的 worker、Critic 的每一轮，全部并进**同一份** ``Usage``；
步数也来自同一条 ``Budget.max_steps``，每一段只从剩余额度里分。``subtask_count`` 与
``review_rounds`` 记在 ``AgentRun`` 上，报告层不必去数轨迹。

判决不在这里
------------
Critic 只决定"这一轮该不该放它走"。harness 仍可能在跑完验证后判 ``fail``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.tools import ToolRegistry

from bench.agent.critic import Critique, ReviewPolicy, parse_verdict
from bench.agent.execution import (
    SubtaskExecution,
    SubtaskOutcome,
    accumulate_subtask_usage,
    ordered_outcomes,
)
from bench.agent.expert import ExpertPool, ToolExpert
from bench.agent.loop import LLMBackedAgent
from bench.agent.planner import (
    Plan,
    PlanParseError,
    Subtask,
    build_plan_message,
    build_plan_review_message,
    parse_plan,
)
from bench.agent.types import (
    AgentRun,
    AgentStatus,
    Budget,
    CodingAgent,
    Step,
    SubtaskUsage,
    Usage,
)
from bench.arms.registry import Arm


@dataclass(frozen=True)
class PlanPolicy:
    """分解与评审的边界。三条上限各自封一类跑飞的方式。"""

    max_subtasks: int = 4
    #: planner 自己的步数上限。分解不探索工作区，默认一次调用收工。
    planner_steps: int = 1
    #: Critic 复核的步数上限（它可能想自己重读、重跑）。
    critic_steps: int = 4


@dataclass(frozen=True)
class _PlanResult:
    """planner 那一段的结果：计划（可能读不出来）加上它的账本与轨迹。"""

    plan: Plan | None
    status: AgentStatus
    usage: Usage
    turns: int
    steps: tuple[Step, ...]
    model: str


@dataclass(frozen=True)
class _ReviewResult:
    """Critic 那一段的结果：裁定（未完成时为 None）加上它的账本。"""

    critique: Critique | None
    status: AgentStatus
    usage: Usage
    turns: int


class L2PlannerWorkerCritic:
    """实现 ``CodingAgent`` 协议的 L2 档。"""

    def __init__(
        self,
        *,
        pool: ExpertPool,
        workspace: Path,
        policy: PlanPolicy | None = None,
        review_policy: ReviewPolicy | None = None,
    ) -> None:
        self._pool = pool
        self._workspace = workspace
        self._policy = policy or PlanPolicy()
        #: 回边硬上限。L2 先只允许一轮重跑（ticket 的"先跑通 handoff"）。
        self._review_policy = review_policy or ReviewPolicy(max_rounds=1)
        #: Critic 就是 L1 用的那个 ``review`` 专家——同一条循环、同一套工具。
        self._critic: ToolExpert = pool.get("review")
        #: worker 与 L0 一样是中性单 agent（专家角色留给多专家档位）。
        self._worker = LLMBackedAgent(pool.llm, pool.tools)
        #: 规划者不探索工作区，只要一次模型调用；机制上仍是同一条工具循环。
        self._planner = LLMBackedAgent(pool.llm, pool.tools)
        #: 采样的温度与本次作答的模型一样，从实际跑循环的那个 client 读——不写死一个
        #: 与配置无关的常数，否则报告里的 temperature 是"实现猜的"，不是"真跑的"。
        self._temperature = pool.temperature

    async def run(self, *, workspace: Path, goal: str, budget: Budget) -> AgentRun:
        combined = Usage()
        turns = 0
        trace: list[Step] = []
        subtask_usage: tuple[SubtaskUsage, ...] = ()

        stopped = budget.stop_status(usage=combined, steps_used=turns)
        if stopped is not None:
            return _finish(
                status=stopped,
                steps=(),
                usage=combined,
                model="",
                temperature=self._temperature,
                subtask_count=0,
                stop_reason=_budget_stop_reason(stopped, budget),
            )

        phase = budget.remaining(usage=combined, steps_used=turns)
        plan_result = await self._plan(workspace=workspace, goal=goal, budget=phase)
        combined = combined + plan_result.usage
        turns += plan_result.turns
        trace.extend(plan_result.steps)
        model = plan_result.model
        plan = plan_result.plan
        if plan is None:
            return _finish(
                status=plan_result.status,
                steps=(),
                usage=combined,
                model=plan_result.model,
                temperature=self._temperature,
                subtask_count=0,
                stop_reason=_plan_stop_reason(plan_result.status),
            )

        stopped = budget.stop_status(usage=combined, steps_used=turns)
        if stopped is not None:
            return _finish(
                status=stopped,
                steps=tuple(_reindex(trace)),
                usage=combined,
                model=model,
                temperature=self._temperature,
                subtask_count=len(plan.subtasks),
                stop_reason=_budget_stop_reason(stopped, budget),
            )

        run = await self._run_subtasks(
            workspace=workspace,
            goal=goal,
            plan=plan,
            budget=budget,
            used_usage=combined,
            used_turns=turns,
            rerun_indices=None,
            critique=None,
            existing=(),
            existing_usage=(),
        )
        combined = combined + run.usage
        turns += run.turns
        model = run.model or model
        trace = [*trace, *run.steps]
        subtask_usage = run.subtask_usage
        run_error = run.error
        if run.status != "completed":
            return _finish(
                status=run.status,
                steps=tuple(_reindex(trace)),
                usage=combined,
                model=model,
                temperature=self._temperature,
                subtask_count=len(plan.subtasks),
                subtask_usage=subtask_usage,
                stop_reason="计划未执行完",
                error=run_error,
            )

        rounds = 0
        while True:
            stopped = budget.stop_status(usage=combined, steps_used=turns)
            if stopped is not None:
                return _finish(
                    status=stopped,
                    steps=tuple(_reindex(trace)),
                    usage=combined,
                    model=model,
                    temperature=self._temperature,
                    subtask_count=len(plan.subtasks),
                    subtask_usage=subtask_usage,
                    review_rounds=rounds,
                    stop_reason=_budget_stop_reason(stopped, budget),
                )
            phase = budget.remaining(usage=combined, steps_used=turns)
            review = await self._review(
                workspace=workspace,
                goal=goal,
                plan=plan,
                outcomes=run.outcomes,
                budget=Budget(
                    max_cost_usd=phase.max_cost_usd,
                    max_steps=min(self._policy.critic_steps, phase.max_steps),
                    max_tokens=phase.max_tokens,
                ),
                max_turns=min(self._policy.critic_steps, phase.max_steps),
            )
            combined = combined + review.usage
            turns += review.turns
            if review.critique is None:
                return _finish(
                    status=review.status,
                    steps=tuple(_reindex(trace)),
                    usage=combined,
                    model=model,
                    temperature=self._temperature,
                    subtask_count=len(plan.subtasks),
                    subtask_usage=subtask_usage,
                    review_rounds=rounds,
                    stop_reason="Critic 未产出终局",
                    error=run_error,
                )
            if review.critique.approved:
                trace.append(Step(index=0, action="finish", observation="Critic 认可"))
                return _finish(
                    status="completed",
                    steps=tuple(_reindex(trace)),
                    usage=combined,
                    model=model,
                    temperature=self._temperature,
                    subtask_count=len(plan.subtasks),
                    subtask_usage=subtask_usage,
                    review_rounds=rounds,
                    error=run_error,
                    stop_reason="全部子任务完成，Critic 认可",
                )
            stopped = budget.stop_status(usage=combined, steps_used=turns)
            if stopped is not None:
                return _finish(
                    status=stopped,
                    steps=tuple(_reindex(trace)),
                    usage=combined,
                    model=model,
                    temperature=self._temperature,
                    subtask_count=len(plan.subtasks),
                    subtask_usage=subtask_usage,
                    review_rounds=rounds,
                    error=run_error,
                    stop_reason=_budget_stop_reason(stopped, budget),
                )
            if rounds >= self._review_policy.max_rounds:
                return _finish(
                    status="step_limit",
                    steps=tuple(_reindex(trace)),
                    usage=combined,
                    model=model,
                    temperature=self._temperature,
                    subtask_count=len(plan.subtasks),
                    subtask_usage=subtask_usage,
                    review_rounds=rounds,
                    error=run_error,
                    stop_reason=(
                        f"Critic 连续打回，已达回边上限 {self._review_policy.max_rounds} 轮"
                    ),
                )

            rounds += 1
            critique = review.critique
            rerun = await self._rerun(
                workspace=workspace,
                goal=goal,
                plan=plan,
                outcomes=run.outcomes,
                critique=critique,
                budget=budget,
                used_usage=combined,
                used_turns=turns,
                existing_usage=run.subtask_usage,
            )
            combined = combined + rerun.usage
            turns += rerun.turns
            if rerun.status != "completed":
                return _finish(
                    status=rerun.status,
                    steps=tuple(_reindex([*trace, *rerun.steps])),
                    usage=combined,
                    model=rerun.model or model,
                    temperature=self._temperature,
                    subtask_count=len(plan.subtasks),
                    subtask_usage=rerun.subtask_usage,
                    review_rounds=rounds,
                    error=rerun.error,
                    stop_reason="重跑未完成",
                )
            run = rerun
            subtask_usage = rerun.subtask_usage
            run_error = rerun.error
            model = rerun.model or model
            trace = [*trace, *rerun.steps]

    # ── 分解 ──────────────────────────────────────────────────────────────

    async def _plan(
        self, *, workspace: Path, goal: str, budget: Budget
    ) -> _PlanResult:
        planner_steps = min(self._policy.planner_steps, budget.max_steps)
        if planner_steps <= 0:
            return _PlanResult(None, "step_limit", Usage(), 0, (), "")
        session = self._planner.session(
            workspace=workspace,
            goal=goal,
            budget=Budget(
                max_cost_usd=budget.max_cost_usd,
                max_steps=planner_steps,
                max_tokens=budget.max_tokens,
            ),
        )
        session.add_user_message(
            build_plan_message(goal=goal, max_subtasks=self._policy.max_subtasks)
        )
        status = await session.advance(max_turns=planner_steps)
        steps = tuple(session.steps)
        if status != "completed":
            return _PlanResult(None, status, session.usage, session.turns, steps, session.model)
        try:
            plan = parse_plan(session.finish_content, max_subtasks=self._policy.max_subtasks)
        except PlanParseError:
            return _PlanResult(
                None, "error", session.usage, session.turns, steps, session.model
            )
        return _PlanResult(plan, status, session.usage, session.turns, steps, session.model)

    async def _rerun(
        self,
        *,
        workspace: Path,
        goal: str,
        plan: Plan,
        outcomes: tuple[SubtaskOutcome, ...],
        critique: Critique,
        budget: Budget,
        used_usage: Usage,
        used_turns: int,
        existing_usage: tuple[SubtaskUsage, ...],
    ) -> SubtaskExecution:
        """按 Critic 的打回重跑整条计划。

        子任务级定向重跑归 L3；L2 只有一个 worker，重跑全量才能保证 Critic 下一轮
        读到的是同一条计划里的新产出。
        """
        return await self._run_subtasks(
            workspace=workspace,
            goal=goal,
            plan=plan,
            budget=budget,
            used_usage=used_usage,
            used_turns=used_turns,
            rerun_indices=self._rerun_targets(plan=plan, critique=critique),
            critique=critique,
            existing=outcomes,
            existing_usage=existing_usage,
        )

    def _rerun_targets(
        self,
        *,
        plan: Plan,
        critique: Critique,
    ) -> tuple[int, ...] | None:
        """L2 全量重跑；L3 覆盖这条，只返回 Critic 点名的子任务。"""
        return None

    async def _run_subtasks(
        self,
        *,
        workspace: Path,
        goal: str,
        plan: Plan,
        budget: Budget,
        used_usage: Usage,
        used_turns: int,
        rerun_indices: "tuple[int, ...] | None",
        critique: "Critique | None",
        existing: tuple[SubtaskOutcome, ...],
        existing_usage: tuple[SubtaskUsage, ...],
    ) -> SubtaskExecution:
        """串行执行（或重跑）子任务。每个子任务一条**新会话**，只带它自己的上下文。

        步数是同一个 ``Budget.max_steps`` 里剩下的额度：每一段开始前先看还剩多少，
        一段用完没有剩余就如实停在 ``step_limit``。
        """
        by_index = {item.index: item for item in existing}
        usage = Usage()
        turns = 0
        steps: list[Step] = []
        model = ""
        usage_by_index = {item.index: item for item in existing_usage}
        wanted = set(rerun_indices) if rerun_indices is not None else None
        order = [
            item for item in plan.subtasks if wanted is None or item.index in wanted
        ]
        for subtask in order:
            combined_usage = used_usage + usage
            stopped = budget.stop_status(
                usage=combined_usage,
                steps_used=used_turns + turns,
            )
            if stopped is not None:
                return SubtaskExecution(
                    outcomes=ordered_outcomes(by_index),
                    usage=usage,
                    turns=turns,
                    steps=tuple(steps),
                    model=model,
                    status=stopped,
                )
            phase = budget.remaining(
                usage=combined_usage,
                steps_used=used_turns + turns,
            )
            handoff = handoff_prompt(
                goal=goal,
                subtask=subtask,
                prior=tuple(
                    by_index[index]
                    for index in sorted(by_index)
                    if index < subtask.index
                ),
                critique=critique,
            )
            session = self._worker.session(workspace=workspace, goal=goal, budget=phase)
            session.add_user_message(handoff)
            status = await session.advance(max_turns=phase.max_steps)
            usage = usage + session.usage
            turns += session.turns
            steps.extend(session.steps)
            fresh_usage = SubtaskUsage(
                index=subtask.index,
                usage=session.usage,
                turns=session.turns,
                status=status,
                model=session.model,
            )
            prior_usage = usage_by_index.get(subtask.index)
            usage_by_index[subtask.index] = (
                fresh_usage
                if prior_usage is None
                else accumulate_subtask_usage(prior_usage, fresh_usage)
            )
            if session.model:
                model = session.model
            if status != "completed":
                return SubtaskExecution(
                    outcomes=ordered_outcomes(by_index),
                    usage=usage,
                    turns=turns,
                    steps=tuple(steps),
                    model=model,
                    status=status,
                    subtask_usage=tuple(
                        usage_by_index[index] for index in sorted(usage_by_index)
                    ),
                    error=session.error,
                )
            by_index[subtask.index] = SubtaskOutcome(
                index=subtask.index,
                instruction=subtask.instruction,
                output=summarize_steps(session.steps),
                trajectory=render_steps(session.steps),
            )
        return SubtaskExecution(
            outcomes=ordered_outcomes(by_index),
            usage=usage,
            turns=turns,
            steps=tuple(steps),
            model=model,
            status="completed",
            subtask_usage=tuple(
                usage_by_index[index] for index in sorted(usage_by_index)
            ),
        )

    # ── 评审 ──────────────────────────────────────────────────────────────

    async def _review(
        self,
        *,
        workspace: Path,
        goal: str,
        plan: Plan,
        outcomes: tuple[SubtaskOutcome, ...],
        budget: Budget,
        max_turns: int,
    ) -> _ReviewResult:
        session = self._critic.session(workspace=workspace, goal=goal, budget=budget)
        session.add_user_message(
            build_plan_review_message(
                goal=goal,
                plan=plan,
                results=tuple((item.output, item.trajectory) for item in outcomes),
            )
        )
        status = await session.advance(max_turns=max_turns)
        if status != "completed":
            return _ReviewResult(None, status, session.usage, session.turns)
        critique = parse_verdict(
            session.finish_content,
            instructions_limit=self._review_policy.max_instructions_chars,
        )
        return _ReviewResult(critique, status, session.usage, session.turns)


def handoff_prompt(
    *,
    goal: str,
    subtask: Subtask,
    prior: tuple[SubtaskOutcome, ...],
    critique: "Critique | None",
) -> str:
    """给 worker 的交接内容：**只有**这条子任务的上下文与前序产出。

    刻意不含整条计划的旁枝、也不含别的子任务的轨迹——handoff 的质量就是这一档在测的
    变量；把它塞满等于替 planner 把活干了。
    """
    lines = [
        f"总目标：{goal}",
        f"你负责的子任务 {subtask.index}：{subtask.instruction}",
    ]
    if subtask.context:
        lines.append(f"背景（planner 附上，动手前先读）：{subtask.context}")
    if subtask.depends_on:
        lines.append(f"依赖的子任务：{list(subtask.depends_on)}")
    if prior:
        lines.append("前面子任务已经产出的结果：")
        for item in prior:
            lines.append(f"  - 子任务 {item.index}：{item.instruction} → {item.output}")
    if critique is not None:
        lines.append(f"上一轮被 Critic 打回，这次必须处理：{critique.instructions}")
    lines.append("完成这条子任务后用 run_tests 验证，然后结束本轮。")
    return "\n".join(lines)


def summarize_steps(steps: tuple[Step, ...] | list[Step]) -> str:
    """一条子任务的产出一句话：最后一个有观察的工具看到什么。"""
    for step in reversed(list(steps)):
        if step.observation:
            return step.observation.strip().replace("\n", " ")[:200]
    return "（没有可记录的产出）"


def render_steps(steps: tuple[Step, ...] | list[Step]) -> str:
    """压缩轨迹：哪一步调了什么、看到了什么。与 L1 的渲染同形，只是更短。"""
    lines: list[str] = []
    for position, step in enumerate(steps, start=1):
        if step.action == "finish":
            continue
        lines.append(f"{position}. {step.tool}（{step.outcome or 'ok'}）")
        if step.observation:
            lines.append(f"   {step.observation.strip()[:200]}")
    return "\n".join(lines) or "（没有工具调用）"


def _reindex(steps: list[Step]) -> list[Step]:
    """把多段会话的轨迹拼成一条：``Step.index`` 是"第几轮"，各会话都从 0 开始。"""
    return [
        Step(
            index=position,
            action=step.action,
            tool=step.tool,
            arguments=step.arguments,
            observation=step.observation,
            outcome=step.outcome,
            elapsed_ms=step.elapsed_ms,
        )
        for position, step in enumerate(steps)
    ]


def _finish(
    *,
    status: AgentStatus,
    steps: tuple[Step, ...],
    usage: Usage,
    model: str,
    temperature: float,
    subtask_count: int,
    subtask_usage: tuple[SubtaskUsage, ...] = (),
    review_rounds: int = 0,
    stop_reason: str = "",
    error: str = "",
) -> AgentRun:
    """L2 的四个结局共用这一个出口——字段少漂一处，账本就少一个对不上的地方。"""
    return AgentRun(
        status=status,
        steps=steps,
        usage=usage,
        model=model,
        temperature=temperature,
        stop_reason=stop_reason,
        error=error,
        review_rounds=review_rounds,
        subtask_count=subtask_count,
        subtask_usage=subtask_usage,
    )


def _plan_stop_reason(status: AgentStatus) -> str:
    if status == "error":
        return "planner 未产出可读的计划"
    return "planner 未完成"


def _budget_stop_reason(status: AgentStatus, budget: Budget) -> str:
    if status == "budget_exhausted":
        return f"累计成本已达全局上限 ${budget.max_cost_usd:.6f}"
    return f"步数上限 {budget.max_steps} 已用完"


def build_l2(*, llm: Any, workspace: Path, tools: ToolRegistry) -> CodingAgent:
    pool = ExpertPool(llm=llm, tools=tools)
    return L2PlannerWorkerCritic(pool=pool, workspace=workspace)


L2 = Arm(
    name="L2",
    description="Planner + 1 worker + Critic：先分解成带上下文的子任务，再逐个交接执行、复核",
    build=build_l2,
)


__all__ = [
    "L2",
    "L2PlannerWorkerCritic",
    "PlanPolicy",
    "SubtaskOutcome",
    "build_l2",
    "handoff_prompt",
    "render_steps",
    "summarize_steps",
]
