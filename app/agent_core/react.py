from __future__ import annotations

import json
import logging
from typing import Protocol

from app.agent_core.types import ReActDecision, TaskStep, TaskResult
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

    async def plan(self, *, task: str) -> list[str]: ...

    async def summarize(
        self, *, task: str, plan: list[str], results: list[TaskResult]
    ) -> str: ...


class ReActLoop:
    """通用 ReAct 循环引擎：规划 → 行动（调工具）→ 观察 → 再规划，直至结束或达到上限。"""

    def __init__(
        self,
        llm: TaskLLM,
        tools: ToolRegistry,
        max_steps: int = 8,
        session_id: str = "",
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._max_steps = max_steps
        self._session_id = session_id

    async def run(self, task: str, subtask: str) -> TaskResult:
        observations: list[str] = []
        steps: list[TaskStep] = []
        tool_calls = 0
        tool_errors = 0

        for i in range(self._max_steps):
            decision = await self._llm.decide(
                task=task, subtask=subtask, observations=observations,
            )
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
                )

            if decision.action == "call_tool":
                tool = self._tools.get(decision.tool_name)
                if tool is None:
                    obs = f"[错误] 工具不存在: {decision.tool_name}"
                    tool_errors += 1
                else:
                    try:
                        args = dict(decision.arguments)
                        # 自动注入 session_id（如果工具声明了该参数）
                        if "session_id" in tool.parameters.get("properties", {}):
                            args.setdefault("session_id", self._session_id)
                        output = await tool.handler(**args)
                        obs = (
                            f"工具 {decision.tool_name}"
                            f"({json.dumps(decision.arguments, ensure_ascii=False)})"
                            f" => {output}"
                        )
                        tool_calls += 1
                    except Exception as exc:
                        obs = f"[错误] 工具 {decision.tool_name} 调用失败: {exc}"
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