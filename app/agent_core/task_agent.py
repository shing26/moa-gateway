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

    @staticmethod
    def _metrics_payload(spent: dict[str, float], model_used: str) -> dict[str, object]:
        return {
            "model_used": model_used,
            "cost_usd": round(spent["cost_usd"], 6),
            "llm_latency_ms": round(spent["llm_latency_ms"], 1),
            "fallback_used": "",
            "prompt_tokens": int(spent["prompt_tokens"]),
            "completion_tokens": int(spent["completion_tokens"]),
        }

    async def execute(self, envelope: AgentEnvelope) -> str:
        raw_task = envelope.user_raw_input
        session_id = envelope.session_id
        logger.info(
            "task agent execute trace=%s session=%s task=%s retry=%d",
            envelope.trace_id, session_id, raw_task[:80], envelope.retry_attempt,
        )

        # 重试归因：上一次失败的原因只拼进本地的 prompt 输入。user_raw_input 是
        # 审计与长期记忆的原始输入，必须保持用户原话，不能被改写。
        task = raw_task
        if envelope.failure_reason:
            task = (
                f"（上一次尝试失败：{envelope.failure_reason}。"
                f"请避开该失败原因，换一种方式完成。）\n{raw_task}"
            )

        # N3：任务链路的 LLM 成本收集器。LLMClient.last_metrics 每次 chat
        # 都会覆盖，因此在 plan / 每轮 ReAct / summarize 后分阶段累加，
        # 最终写回 envelope 供 pipeline / graph 记账与预算累计（与
        # stubs.capture_metrics 消费同一条 llm_metrics 通道）。
        spent = {"cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0.0, "llm_latency_ms": 0.0}
        model_used = ""

        def _collect() -> None:
            nonlocal model_used
            client = getattr(self._llm, "_client", None)
            metrics = getattr(client, "last_metrics", None)
            if not metrics:
                return
            spent["cost_usd"] += float(metrics.get("cost_usd", 0.0) or 0.0)
            spent["prompt_tokens"] += int(metrics.get("prompt_tokens", 0) or 0)
            spent["completion_tokens"] += int(metrics.get("completion_tokens", 0) or 0)
            spent["llm_latency_ms"] += float(metrics.get("llm_latency_ms", 0.0) or 0.0)
            model_used = str(metrics.get("model_used", "")) or model_used

        try:
            # 1. 规划
            plan = await self._llm.plan(task=task)
            _collect()
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
                _collect()
                results.append(result)

            # 3. 汇总
            answer = await self._llm.summarize(task=task, plan=plan, results=results)
            _collect()
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
                "task agent done trace=%s tool_calls=%d cost_usd=%s",
                envelope.trace_id, total_tools, spent["cost_usd"],
            )
            return answer
        finally:
            # 写在 finally 里：失败的尝试同样烧了 token，不能因为这一轮没成功就
            # 丢掉成本（重试时 app/agents/retry.py 会把各次尝试的 llm_metrics 累加）。
            envelope.agent_local_slot["llm_metrics"] = self._metrics_payload(spent, model_used)


register_agent("task", TaskAgent())

__all__ = ["TaskAgent"]