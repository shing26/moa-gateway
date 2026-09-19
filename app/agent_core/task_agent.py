from __future__ import annotations

import logging
import os

from app.agent_core.mock_llm import MockTaskLLM
from app.agent_core.react import ReActLoop, TaskLLM
from app.agent_core.types import TaskResult
from app.agents.contract import AgentEnvelope, register_agent
from app.agents.tools import tool_registry

# 注册扩展工具（calculator / list_documents / add_note / get_notes）：
# 该模块在 import 时即执行注册，必须显式导入，否则生产环境拿不到这些工具。
import app.agent_core.tools_extra  # noqa: F401

logger = logging.getLogger("moa.agent_core.task_agent")

_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))


def _build_task_llm() -> TaskLLM:
    mode = os.environ.get("AGENT_LLM", "mock").lower()
    if mode == "litellm":
        try:
            from app.agent_core.litellm_llm import LiteLLMTaskLLM

            llm = LiteLLMTaskLLM()
            logger.info("task agent using LiteLLM backend")
            return llm
        except Exception as exc:
            logger.warning("litellm task llm init failed, falling back to mock: %s", exc)
    return MockTaskLLM()


class TaskAgent:
    """自主任务 Agent：接收任务 → 规划 → 多轮 ReAct 执行 → 汇总汇报。

    实现现有 ``SubAgent`` 协议（``execute(envelope) -> str``），注册进
    ``AGENT_REGISTRY`` 后，由 pipeline 自动接入守卫 / HITL / 审计 / 记忆。
    默认使用离线 Mock LLM（``AGENT_LLM=mock``），工具与循环为真实实现。
    """

    def __init__(
        self, llm: TaskLLM | None = None, max_steps: int = _MAX_STEPS
    ) -> None:
        self._llm = llm or _build_task_llm()
        self._max_steps = max_steps

    async def execute(self, envelope: AgentEnvelope) -> str:
        task = envelope.user_raw_input
        session_id = envelope.session_id
        logger.info(
            "task agent execute trace=%s session=%s task=%s",
            envelope.trace_id, session_id, task[:80],
        )

        # 1. 规划
        plan = await self._llm.plan(task=task)
        envelope.agent_local_slot["plan"] = list(plan)
        logger.info("task agent plan=%s", plan)

        # 2. 每个子任务跑 ReAct 循环
        results: list[TaskResult] = []
        for subtask in plan:
            loop = ReActLoop(
                self._llm,
                tool_registry,
                max_steps=self._max_steps,
                session_id=session_id,
            )
            result = await loop.run(task=task, subtask=subtask)
            results.append(result)

        # 3. 汇总
        answer = await self._llm.summarize(task=task, plan=plan, results=results)
        envelope.agent_local_slot["task_results"] = [
            [
                {
                    "index": s.index,
                    "action": s.decision.action,
                    "tool": s.decision.tool_name,
                    "note": s.decision.note,
                    "observation": s.observation,
                }
                for s in r.steps
            ]
            for r in results
        ]
        total_tools = sum(r.tool_calls for r in results)
        envelope.agent_local_slot["tool_calls_total"] = total_tools
        logger.info(
            "task agent done trace=%s tool_calls=%d", envelope.trace_id, total_tools,
        )
        return answer


register_agent("task", TaskAgent())

__all__ = ["TaskAgent"]