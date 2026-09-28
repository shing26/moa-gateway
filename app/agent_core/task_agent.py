from __future__ import annotations

import logging

from app.agent_core.mock_llm import MockTaskLLM
from app.agent_core.react import ReActLoop, TaskLLM
from app.agent_core.types import TaskResult
from app.agents.contract import AgentEnvelope, register_agent
from app.agents.tools import tool_registry
from app.config import settings

# 注册扩展工具（calculator / list_documents / add_note / get_notes）：
# 该模块在 import 时即执行注册，必须显式导入，否则生产环境拿不到这些工具。
import app.agent_core.tools_extra  # noqa: F401

logger = logging.getLogger("moa.agent_core.task_agent")


def _build_task_llm() -> TaskLLM:
    # 后端与步数上限都从 app/config.py 取（此前直读 os.environ，绕过配置层，
    # 且与 app/agents/stubs.py 的 MAX_TOOL_ROUNDS 是两个值）。
    mode = settings.agent_llm
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
        self, llm: TaskLLM | None = None, max_steps: int | None = None
    ) -> None:
        self._llm = llm or _build_task_llm()
        # 不在这里给字面默认值：唯一来源是 settings.agent_max_steps，
        # 免得又出现"同一概念两个值"（此前这里是 8，stubs 是 3）。
        self._max_steps = settings.agent_max_steps if max_steps is None else max_steps

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

        # 降级原因收集器：每请求一个。`self._llm` 是进程内单例（模块导入时构造一次），
        # 把降级原因挂在它身上会被并发请求互相踩，所以只能随本次调用走
        # （ADR-018 决策 7）。
        degradations: list[str] = []
        try:
            # 1. 规划
            plan = await self._llm.plan(task=task, on_degrade=degradations.append)
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
                # 子任务内部的决策降级由 ReActLoop 带回来（LLM 调用失败 / 输出不是
                # 合法 JSON 时它以 finish 兜底，但那不是模型决定收尾）。
                degradations.extend(result.degraded_reasons)
                results.append(result)

            # 3. 汇总
            answer = await self._llm.summarize(
                task=task, plan=plan, results=results,
                on_degrade=degradations.append,
            )
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
            total_tool_errors = sum(r.tool_errors for r in results)
            # 参数被拒与被执行失败分开记（ADR-018 决策 3）：前者是模型填错、后者是
            # 环境问题，合成一个数就分不出"模型在瞎猜参数"与"后端挂了"。
            total_rejections = sum(r.tool_arg_rejections for r in results)
            envelope.agent_local_slot["tool_calls_total"] = total_tools
            # 工具失败必须留下计数：否则"任务真的完成"与"所有工具都失败但优雅降级"
            # 在审计里长得一模一样（2026-09-22 外部评估指出的可观测性缺口）。
            envelope.agent_local_slot["tool_errors_total"] = total_tool_errors
            envelope.agent_local_slot["tool_arg_rejections_total"] = total_rejections
            # 只在真降级时写：这个键的缺席本身有含义（"这次没降级"），与写空列表
            # 不同——pipeline 据此补一条 issue 转人工（ADR-018 决策 7）。
            if degradations:
                envelope.agent_local_slot["task_degradation_reasons"] = list(degradations)
            logger.info(
                "task agent done trace=%s tool_calls=%d tool_errors=%d "
                "tool_arg_rejections=%d degraded=%d cost_usd=%s",
                envelope.trace_id, total_tools, total_tool_errors, total_rejections,
                len(degradations), spent["cost_usd"],
            )
            return answer
        finally:
            # 写在 finally 里：失败的尝试同样烧了 token，不能因为这一轮没成功就
            # 丢掉成本（重试时 app/agents/retry.py 会把各次尝试的 llm_metrics 累加）。
            envelope.agent_local_slot["llm_metrics"] = self._metrics_payload(spent, model_used)


register_agent("task", TaskAgent())

__all__ = ["TaskAgent"]