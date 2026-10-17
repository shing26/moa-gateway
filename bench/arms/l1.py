"""L1：§5.0 的单 agent 加一条 **有界** Critic 回边（§5 四档表）。

与 L0 的唯一差别是回边
----------------------
worker 是同一个 ``LLMBackedAgent``（同一套工具、同一段任务指令、同一个停止条件）；
Critic 是 §5.1 专家池里的 ``review`` 专家——它本身就是「角色指令 + 同一条
``LLMBackedAgent`` + 同一套工具」，所以能自己重读文件、重跑验证，不必为 Critic
另造第三套循环（ticket 04 已把它做出来，这里直接用）。

为什么 Critic 的批评能落成"下一轮的上下文"
------------------------------------------
回边复用**同一条** ``LoopSession``：Critic 的指令作为一条 user 消息追加进去，
worker 接着上一轮的历史继续，而不是另起一次 ``run()`` 把前文丢掉。这正是 §5.0
说的"知道自己已经试过什么，不重复撞同一面墙"。

账本是同一份
------------
§5.3 把协调开销算进协作成本，所以 Critic 的 ``llm_calls`` / ``cost_usd`` 与 worker
进**同一份** ``Usage``：worker 的会话跨回边累计，Critic 每轮的用量再并进来。
步数也是同一条 ``Budget.max_steps``——给 Critic 留步数靠"从剩余额度里分"。

判决不在这里
------------
Critic 只决定"这一轮该不该放它走"，不是判决。agent 自报 ``completed`` 之后 harness
仍可能判 ``fail``（CONTEXT.md「执行体与判定」）。Critic 拿不到 ``verdict_exit_code``，
也不去猜。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.tools import ToolRegistry

from bench.agent.critic import (
    Critique,
    ReviewPolicy,
    build_review_message,
    format_revise_instruction,
    parse_verdict,
)
from bench.agent.expert import ExpertPool, ToolExpert
from bench.agent.loop import LLMBackedAgent, LoopSession
from bench.agent.types import AgentRun, Budget, CodingAgent, Usage
from bench.arms.registry import Arm


class L1CriticAgent:
    """实现 ``CodingAgent`` 协议的 L1 档。"""

    def __init__(
        self,
        *,
        pool: ExpertPool,
        workspace: Path,
        policy: ReviewPolicy | None = None,
    ) -> None:
        self._workspace = workspace
        self._policy = policy or ReviewPolicy()
        #: Critic 就是池里的 ``review`` 专家（能重读、能重跑），不是第二套循环。
        self._critic: ToolExpert = pool.get("review")
        # worker 是中性单 agent（L1 的"唯一差别是回边"，所以不给它专家角色）。
        # 它取工具的唯一来源仍是注入的那个 registry——池与 worker 共用一份。
        self._worker = LLMBackedAgent(pool.llm, pool.tools)

    async def run(self, *, workspace: Path, goal: str, budget: Budget) -> AgentRun:
        # worker 的会话跨回边累计（历史、轨迹、用量），Critic 每轮各开一条。
        worker = self._worker.session(workspace=workspace, goal=goal, budget=budget)
        worker_status = await worker.advance()
        if worker_status != "completed":
            return worker.build_run(worker_status)

        rounds = 0
        combined = worker.usage
        while True:
            remaining = budget.max_steps - worker.turns
            if remaining <= 0:
                return worker.build_run(
                    "step_limit",
                    usage=combined,
                    review_rounds=rounds,
                    stop_reason=f"步数上限 {budget.max_steps} 已用完，没有步数给 Critic",
                )

            critique, review_status, review_usage = await self._review(
                goal=goal, steps=worker.steps, budget=budget, max_turns=remaining
            )
            combined = _merge_usage(combined, review_usage)
            if review_status != "completed":
                return worker.build_run(
                    review_status,
                    usage=combined,
                    review_rounds=rounds,
                    stop_reason="Critic 未产出终局",
                )
            if critique is None:
                # 到不了：review_status == "completed" 时终局一定解析出了裁定。
                raise AssertionError("review_status=completed 却没有裁定")
            if critique.approved:
                return _finalize(worker, combined, review_rounds=rounds)

            if rounds >= self._policy.max_rounds:
                return worker.build_run(
                    "step_limit",
                    usage=combined,
                    review_rounds=rounds,
                    stop_reason=(
                        f"Critic 连续打回，已达回边上限 {self._policy.max_rounds} 轮"
                    ),
                )

            worker.add_user_message(format_revise_instruction(critique.instructions))
            rounds += 1
            worker_status = await worker.advance(
                max_turns=max(0, budget.max_steps - worker.turns)
            )
            if worker_status != "completed":
                return worker.build_run(worker_status, usage=combined, review_rounds=rounds)

    async def _review(
        self, *, goal: str, steps: Any, budget: Budget, max_turns: int
    ) -> "tuple[Critique | None, str, Usage]":
        """跑 Critic 一轮：给它目标 + worker 的轨迹，收它的终局与用量。

        终局不是 ``completed``（超步数 / 超预算 / 报错）时 ``Critique`` 为 ``None``：
        没有可信的裁定可以往下传，调用方按对应的自报状态收尾。
        """
        session = self._critic.session(workspace=self._workspace, goal=goal, budget=budget)
        session.add_user_message(build_review_message(goal=goal, steps=tuple(steps)))
        status = await session.advance(max_turns=max_turns)
        if status != "completed":
            return None, status, session.usage
        critique = parse_verdict(
            session.finish_content,
            instructions_limit=self._policy.max_instructions_chars,
        )
        return critique, status, session.usage


def _merge_usage(left: Usage, right: Usage) -> Usage:
    """把 Critic 的账并进 worker 的账。协调开销是协作成本的一部分（§5.3）。"""
    return Usage(
        llm_calls=left.llm_calls + right.llm_calls,
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        cost_usd=left.cost_usd + right.cost_usd,
        llm_latency_ms=left.llm_latency_ms + right.llm_latency_ms,
    )


def _finalize(worker: LoopSession, combined: Usage, *, review_rounds: int) -> AgentRun:
    """Critic 认可：worker 的轨迹不变，只把合并后的账本与轮数写进去。"""
    return worker.build_run(
        "completed",
        usage=combined,
        review_rounds=review_rounds,
        stop_reason="模型声明完成，Critic 认可",
    )


def build_l1(*, llm: Any, workspace: Path, tools: ToolRegistry) -> CodingAgent:
    """注册表统一构造形状：worker 与 Critic 共用同一个 client 与同一套工具。

    串行档（L1）共用 client 没问题；L3 并发时每个 worker 要各自独立的 client，那条
    由 L3 自己安排（``last_metrics`` 是单值通道，共用会把账串掉）。
    """
    pool = ExpertPool(llm=llm, tools=tools)
    return L1CriticAgent(pool=pool, workspace=workspace)


L1 = Arm(
    name="L1",
    description="单体 + Critic 有界回边：worker 自报完成后由 review 专家复核，打回就按指令再改",
    build=build_l1,
)

__all__ = ["L1", "L1CriticAgent", "build_l1"]
