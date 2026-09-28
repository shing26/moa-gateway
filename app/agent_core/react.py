from __future__ import annotations

import json
import logging
from typing import Protocol

from app.agent_core.types import DegradeCallback, ReActDecision, TaskStep, TaskResult
from app.agents.tool_contract import call_tool
from app.agents.tools import ToolRegistry

logger = logging.getLogger("moa.agent_core.react")


class TaskLLM(Protocol):
    """ReAct 循环所需的 LLM 接口：

    - plan() 将任务拆解为子任务列表
    - decide() 对当前子任务做出下一步决策（调工具 / 结束）
    - summarize() 汇总所有子任务结果
    """

    async def decide(
        self, *, task: str, subtask: str, observations: list[str]
    ) -> ReActDecision: ...

    async def plan(
        self, *, task: str, on_degrade: DegradeCallback | None = None
    ) -> list[str]: ...

    async def summarize(
        self,
        *,
        task: str,
        plan: list[str],
        results: list[TaskResult],
        on_degrade: DegradeCallback | None = None,
    ) -> str: ...


class ReActLoop:
    """通用 ReAct 循环引擎：规划 → 行动（调工具）→ 观察 → 再规划，直至结束或达到上限。"""

    def __init__(
        self,
        llm: TaskLLM,
        tools: ToolRegistry,
        max_steps: int | None = None,
        session_id: str = "",
    ) -> None:
        self._llm = llm
        self._tools = tools
        # 默认跟随 settings.agent_max_steps（工具轮次/步数的唯一上限）。
        # 此前这里写死 8，而 app/agents/stubs.py 是 3 —— 同一概念两个值。
        if max_steps is None:
            from app.config import settings

            max_steps = settings.agent_max_steps
        self._max_steps = max_steps
        self._session_id = session_id

    async def run(self, task: str, subtask: str) -> TaskResult:
        observations: list[str] = []
        steps: list[TaskStep] = []
        tool_calls = 0
        tool_errors = 0
        tool_arg_rejections = 0
        degraded_reasons: list[str] = []

        for i in range(self._max_steps):
            decision = await self._llm.decide(
                task=task, subtask=subtask, observations=observations,
            )
            # 降级兜底（LLM 调用失败 / 输出不是合法 JSON）也走 finish，但它不是
            # "模型决定收尾"。记下来，别让它长得像正常回答（ADR-018 决策 7）。
            if decision.degraded_reason:
                degraded_reasons.append(decision.degraded_reason)
            logger.info(
                "react step=%d action=%s tool=%s note=%s",
                i, decision.action, decision.tool_name, decision.note,
            )

            if decision.action == "finish":
                steps.append(TaskStep(index=i, decision=decision))
                answer = decision.final_answer or self._build_fallback_answer(
                    subtask, observations,
                )
                return TaskResult(
                    answer=answer, steps=steps, plan=[subtask],
                    tool_calls=tool_calls, tool_errors=tool_errors,
                    tool_arg_rejections=tool_arg_rejections,
                    degraded_reasons=degraded_reasons,
                )

            if decision.action == "call_tool":
                # tool_calls 是**尝试数**，在分支入口自增：成功、失败、被拒都算一次
                # 尝试。判据「全部失败」（tool_errors + 被拒 == tool_calls）只有在
                # 尝试数口径下才成立——此前它记的是**成功数**，于是真·全失败
                # （tool_calls=0）反而不触发刹车，而"2 成功 + 2 失败"却触发
                # （见 ADR-018「过程发现」）。
                tool_calls += 1
                tool = self._tools.get(decision.tool_name)
                if tool is None:
                    obs = f"[错误] 工具不存在: {decision.tool_name}"
                    tool_errors += 1
                else:
                    # 注入（系统值覆盖模型值）→ 校验 → 执行，都在 tool_contract 里
                    # 完成：两条调用路径共用同一份契约（ADR-018）。
                    outcome = await call_tool(
                        tool, decision.arguments, session_id=self._session_id
                    )
                    if outcome.kind == "ok":
                        obs = (
                            f"工具 {decision.tool_name}"
                            f"({json.dumps(decision.arguments, ensure_ascii=False)})"
                            f" => {outcome.detail}"
                        )
                    elif outcome.kind == "rejected":
                        # 参数被拒 ≠ 执行失败。模型填错参数是它自己能修的，必须独立
                        # 计数，否则"模型输错了"与"环境挂了"在指标上分不开。
                        obs = (
                            f"[参数被拒] 工具 {decision.tool_name}: {outcome.detail}。"
                            "请修正参数后重试。"
                        )
                        tool_arg_rejections += 1
                    else:
                        obs = f"[错误] 工具 {decision.tool_name} 调用失败: {outcome.detail}"
                        tool_errors += 1
                observations.append(obs)
                steps.append(TaskStep(index=i, decision=decision, observation=obs))

        # 达到最大步数 — 返回部分结果
        final = (
            "已达最大步数上限，部分任务未完成。\n\n已执行步骤:\n"
            + "\n".join(
                f"{s.index + 1}. {s.decision.note or s.decision.tool_name}"
                for s in steps
            )
        )
        return TaskResult(
            answer=final, steps=steps, plan=[subtask],
            tool_calls=tool_calls, tool_errors=tool_errors,
            tool_arg_rejections=tool_arg_rejections,
            degraded_reasons=degraded_reasons,
        )

    @staticmethod
    def _build_fallback_answer(subtask: str, observations: list[str]) -> str:
        if not observations:
            return (
                f"关于「{subtask}」：已理解你的请求，但暂时无法处理，"
                "请提供更具体的信息。"
            )
        lines = [f"关于「{subtask}」的完成结果："] + observations
        return "\n\n".join(lines)