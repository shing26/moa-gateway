"""§5.0 的循环：观察 → 决策 → 行动 → 观察。

停止条件只有四个结局（``AgentStatus``），它们的判定顺序是刻意的：

1. 本轮模型没再要求调工具 ⇒ ``completed``（**先判完成**）；
2. 否则累计成本已达上限 ⇒ ``budget_exhausted``；
3. 否则继续，步数用尽 ⇒ ``step_limit``；
4. LLM 调用抛错 ⇒ ``error``。

第 1、2 条的先后顺序是重点：模型刚好在预算越顶的那一轮交了终局，那它**确实
完成了**，记成 ``budget_exhausted`` 是把成功说成失败。反之"想继续但没钱了"
才是 §8 说的那个必须与 ``fail`` 分开的信号。

美元预算只能事后累计（调用前无从得知这次花多少），所以实现上允许最后一次
调用把预算越过一点；判据是"累计值已达到上限"。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from app.agents.tool_contract import OutcomeKind, ToolCallOutcome
from app.agents.tools import ToolRegistry

from bench.agent.memory import MemoryPolicy, compact_messages, truncate_observation
from bench.agent.tools import call_bench_tool
from bench.agent.types import AgentRun, AgentStatus, Budget, Step, Usage

logger = logging.getLogger("bench.agent.loop")


def _system_prompt(workspace: Path, goal: str, instructions: str = "") -> str:
    base = (
        "你是一个自主编码 agent，在一个代码工作区里工作。\n"
        f"工作区根目录：{workspace}\n"
        f"目标：{goal}\n\n"
        "工作方式：每一步只做一件事（读文件、改文件、或跑测试），根据结果决定下一步。\n"
        "认为目标已达成时，直接结束本轮（不再调用任何工具）。\n"
        "不要臆测文件内容，先读再看；不要重复已经失败过的做法。"
    )
    # 角色指令由调用方给（§5.1 的专家），追加而不是替换：循环的规矩对每个角色
    # 都成立，角色只多说一层"你是谁、你擅长什么"。
    if instructions:
        return f"{base}\n\n{instructions}"
    return base


class LLMBackedAgent:
    """单 agent（§5.0）。L0 档就是它跑一份。"""

    def __init__(
        self,
        llm: Any,
        tools: ToolRegistry,
        *,
        policy: MemoryPolicy | None = None,
        model: str = "",
        temperature: float | None = None,
        instructions: str = "",
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._policy = policy or MemoryPolicy()
        self._instructions = instructions
        config = getattr(llm, "config", None)
        self._model = model or str(getattr(config, "model", "") or "")
        if temperature is None:
            raw = getattr(config, "temperature", 0.0)
            temperature = float(raw if raw is not None else 0.0)
        self._temperature = temperature

    async def run(self, *, workspace: Path, goal: str, budget: Budget) -> AgentRun:
        schemas = self._tools.list_schemas()
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": _system_prompt(workspace, goal, self._instructions),
            },
            {"role": "user", "content": goal},
        ]
        steps: list[Step] = []
        usage = Usage()
        #: 实际作答的模型。fallback 链接手时 ``last_metrics["model_used"]`` 与配置的
        #: 主模型不同——如实记录是 §12 的要求（"RunResult 如实记录 model"）。
        model = self._model

        for index in range(max(0, budget.max_steps)):
            try:
                result = await self._llm.chat_with_tools(messages, schemas)
            except Exception as exc:  # noqa: BLE001 - 基础设施故障就是 error 这条腿
                logger.warning("agent llm call failed: %s", exc)
                return self._stop(
                    steps,
                    usage,
                    model,
                    "error",
                    stop_reason="LLM 调用失败",
                    error=f"{type(exc).__name__}: {exc}",
                )

            metrics = getattr(self._llm, "last_metrics", None)
            usage = usage.add_call(metrics)
            reported = str((metrics or {}).get("model_used") or "").strip()
            if reported:
                model = reported

            # ① 终局：模型不再要求调工具。**先于预算判定**，理由见模块 docstring。
            if not result.tool_calls:
                steps.append(
                    Step(index=index, action="finish", observation=str(result.content or "")[:200])
                )
                return self._stop(
                    steps, usage, model, "completed", stop_reason="模型声明完成"
                )

            # ② 想让继续，但钱不够了。
            if usage.cost_usd >= budget.max_cost_usd:
                logger.info(
                    "budget exhausted cost=%.6f limit=%.6f", usage.cost_usd, budget.max_cost_usd
                )
                return self._stop(
                    steps,
                    usage,
                    model,
                    "budget_exhausted",
                    stop_reason=(
                        f"累计成本 ${usage.cost_usd:.6f} 已达上限 ${budget.max_cost_usd:.6f}"
                    ),
                )

            # 自己拼 assistant 消息，不依赖 provider 的 messages 回传格式——
            # 两条执行路径（真 LLM / 脚本）因此走的是同一段历史维护代码。
            assistant: dict[str, Any] = {"role": "assistant", "content": result.content or ""}
            assistant["tool_calls"] = list(result.tool_calls)
            messages.append(assistant)

            for call in result.tool_calls:
                step, tool_message = await self._execute_call(
                    call, index=index, workspace=workspace
                )
                steps.append(step)
                messages.append(tool_message)

            messages = compact_messages(messages, self._policy)

        return self._stop(
            steps,
            usage,
            model,
            "step_limit",
            stop_reason=f"已达步数上限 {budget.max_steps}",
        )

    def _stop(
        self,
        steps: list[Step],
        usage: Usage,
        model: str,
        status: AgentStatus,
        *,
        stop_reason: str = "",
        error: str = "",
    ) -> AgentRun:
        """四个结局共用这一个出口：字段少漂一处，账本就少一个对不上的地方。"""
        return AgentRun(
            status=status,
            steps=tuple(steps),
            usage=usage,
            model=model,
            temperature=self._temperature,
            stop_reason=stop_reason,
            error=error,
        )

    async def _execute_call(
        self, call: dict[str, Any], *, index: int, workspace: Path
    ) -> tuple[Step, dict[str, Any]]:
        function = call.get("function") or {}
        name = str(function.get("name", ""))
        raw_arguments = function.get("arguments")
        call_id = str(call.get("id", name or "call"))
        started = time.monotonic()

        if isinstance(raw_arguments, dict):
            arguments = dict(raw_arguments)
        else:
            try:
                arguments = json.loads(raw_arguments or "{}")
            except (TypeError, ValueError) as exc:
                # 模型给的参数不是合法 JSON：算**被拒**（它自己填错了），不是环境坏了。
                return self._observation_step(
                    index=index,
                    name=name,
                    arguments={},
                    outcome="rejected",
                    detail=f"参数不是合法 JSON: {exc}",
                    call_id=call_id,
                    started=started,
                )
        if not isinstance(arguments, dict):
            return self._observation_step(
                index=index,
                name=name,
                arguments={},
                outcome="rejected",
                detail="参数必须是 JSON 对象",
                call_id=call_id,
                started=started,
            )

        tool = self._tools.get(name)
        if tool is None:
            return self._observation_step(
                index=index,
                name=name,
                arguments=arguments,
                outcome="failed",
                detail=f"工具不存在: {name}",
                call_id=call_id,
                started=started,
            )

        outcome: ToolCallOutcome = await call_bench_tool(
            tool, arguments, workspace=workspace
        )
        return self._observation_step(
            index=index,
            name=name,
            arguments=arguments,
            outcome=outcome.kind,
            detail=outcome.detail,
            call_id=call_id,
            started=started,
        )

    def _observation_step(
        self,
        *,
        index: int,
        name: str,
        arguments: dict[str, Any],
        outcome: OutcomeKind,
        detail: str,
        call_id: str,
        started: float,
    ) -> tuple[Step, dict[str, Any]]:
        """把一次工具结局收成（轨迹步, 回给模型的 tool 消息）。

        ok / rejected / failed 三种走同一条路：观察截断与 tool_call_id 配对是它们
        共同的部分，只有 outcome 与 detail 不同。
        """
        observation = truncate_observation(detail, self._policy)
        step = Step(
            index=index,
            action="call_tool",
            tool=name,
            arguments=arguments,
            observation=observation,
            outcome=outcome,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        return step, {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": observation,
        }


__all__ = ["LLMBackedAgent"]
