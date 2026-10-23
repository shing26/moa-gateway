"""§5.0 的循环：观察 → 决策 → 行动 → 观察。

停止条件只有四个结局（``AgentStatus``），它们的判定顺序是刻意的：

1. 本轮模型没再要求调工具 ⇒ ``completed``（**先判完成**）；
2. 否则累计成本已达上限 ⇒ ``budget_exhausted``；
3. 否则继续，步数用尽 ⇒ ``step_limit``；
4. LLM 调用抛错、且重试预算耗尽 ⇒ ``error``。

第 1、2 条的先后顺序是重点：模型刚好在预算越顶的那一轮交了终局，那它**确实
完成了**，记成 ``budget_exhausted`` 是把成功说成失败。反之"想继续但没钱了"
才是 §8 说的那个必须与 ``fail`` 分开的信号。

美元预算只能事后累计（调用前无从得知这次花多少），所以实现上允许最后一次
调用把预算越过一点；判据是"累计值已达到上限"。

``error`` 前面有一层**有界重试**：免费/预览端点偶发 500（``Failed to generate
completions``）是 §8 所说的噪音腿，一次抖动就把整条 run 判成 ``error``，长曲线
会被噪声淹没。重试只在**调用失败**时发生，不改四个结局的语义——耗尽后照旧落
``error``。工具异常不走这条（它们降级成 observation，见 ``execute_call``）。

``LoopSession`` 是这条循环的续跑缝
----------------------------------
循环原本一次性：``run()`` 走到 ``completed`` 就返回。L1 的有界回边要"接着上一段
继续"，所以历史与循环体被拆进 ``LoopSession``，``LLMBackedAgent.run()`` 只是它的
一次 ``advance()``。L0 的轨迹与账本因此逐字不变（``test_bench_agent_loop.py`` 是
那道门）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol

from app.agents.tool_contract import OutcomeKind, ToolCallOutcome
from app.agents.tools import ToolRegistry

from bench.agent.memory import MemoryPolicy, compact_messages, truncate_observation
from bench.agent.tools import call_bench_tool
from bench.agent.types import (
    AgentRun,
    AgentStatus,
    Budget,
    LLMRetryEvent,
    Step,
    Usage,
)

logger = logging.getLogger("bench.agent.loop")

#: 一次 LLM 调用允许的尝试次数（含首次）。免费端点实测约 2% 的调用会 500，
#: 40 步的 run 因此有近一半概率被一次抖动杀掉——重试把这条噪声腿压下去。
LLM_RETRY_ATTEMPTS = 3
#: 每次重试前的退避（毫秒），最后一次的值会重复用于更后面的尝试。
LLM_RETRY_BACKOFF_MS = (1000, 3000)


def _rate_limit_status(exc: BaseException) -> int | None:
    """从 provider 异常里提取可复核的限流状态，不保存请求正文。"""
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "http_status", None),
        getattr(exc, "code", None),
    ):
        try:
            status = int(value)
        except (TypeError, ValueError):
            continue
        if status > 0:
            return status
    label = f"{type(exc).__name__}: {exc}".lower()
    return 429 if "ratelimit" in label or "too many requests" in label else None


class _ToolRunner(Protocol):
    """会话驱动的循环机制（§5.0 决策 3 的原生 tool-calling）。

    ``LLMBackedAgent`` 满足它；把它抽出来只是让 ``LoopSession`` 依赖"循环机制"
    这个概念，而不是某个具体类——专家（§5.1）将来要接别的 runner 时不用改会话。
    """

    _llm: Any
    _tools: ToolRegistry
    _policy: MemoryPolicy
    _model: str
    _temperature: float

    async def execute_call(
        self, call: dict[str, Any], *, index: int, workspace: Path
    ) -> "tuple[Step, dict[str, Any]]": ...


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


class LoopSession:
    """一条可被多次推进的工具循环，持有自己的消息历史、轨迹与账本。

    §5.0 的循环原本是一次性的：``run()`` 走到 ``completed`` 就返回，没有"接着上一段
    继续"的入口。L1 的有界回边需要它——Critic 的修正指令要加进**同一条**会话，否则
    要么另起一次 ``run()`` 丢掉前文（"别重复撞同一面墙"就没了），要么把 L1 的逻辑
    塞进 L0 的循环里。所以"继续"这条缝开在这里：``LLMBackedAgent.run()`` 只是一次
    ``LoopSession.advance()``，L1 则持有同一个会话反复推进。

    会话**不**记录 ``finish`` 步：一次 run 的终局只有调用方知道（L1 的第一次
    ``completed`` 其实是被打回前的半程）。调用方在收尾时补一条，轨迹上就只有一条
    真实的终局。

    ``turns`` 与 ``usage`` 跨多次 ``advance()`` 累计——L1 把 worker 与 Critic 的
    用量合进同一份账本靠的就是这两个是"会话级"而不是"单次推进级"的。
    """

    def __init__(
        self,
        runner: _ToolRunner,
        *,
        workspace: Path,
        goal: str,
        budget: Budget,
        instructions: str = "",
    ) -> None:
        self._runner = runner
        self._workspace = workspace
        self._policy = runner._policy
        #: 整条会话的硬上限。``max_steps`` 是唯一的天花板，续跑接着算。
        self._budget = budget
        #: 实际作答的模型。fallback 链接手时 ``last_metrics["model_used"]`` 与配置的
        #: 主模型不同——如实记录是 §12 的要求（"RunResult 如实记录 model"）。
        self.model = runner._model
        #: 产出这次 run 的专家（§5.1）。会话本身不贴标签，由专家层在收尾时补。
        self.expert = ""
        self.steps: list[Step] = []
        self.usage = Usage()
        #: 已走的工具轮次，跨 ``advance()`` 累计。
        self.turns = 0
        #: 已发出的 LLM 调用数（含终局那一轮，它不占工具轮次）。
        self.served_calls = 0
        #: 最后一次调用模型说的那句收工话——``finish`` 步的观察。
        self.finish_content = ""
        #: 最后一次调用的 provider 停止原因与 reasoning 长度。它们不能只留在
        #: ``last_metrics`` 里：下一次调用会覆盖，复盘空输出时就没有边界证据了。
        self.finish_reason = ""
        self.reasoning_chars = 0
        #: 最近一次 ``error`` 的原文。
        self.error = ""
        self._last_index = 0
        self._messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": _system_prompt(workspace, goal, instructions),
            },
            {"role": "user", "content": goal},
        ]

    @property
    def last_turn_index(self) -> int:
        """最后服务过的那一轮的序号——调用方补 ``finish`` 步时用它对齐。"""
        return self._last_index

    @property
    def max_steps(self) -> int:
        return max(0, self._budget.max_steps)

    @property
    def max_cost_usd(self) -> float:
        return self._budget.max_cost_usd

    @property
    def max_tokens(self) -> int:
        """token 兜底上限；``0`` 表示不设（§5.3：它不是主线判据）。"""
        return self._budget.max_tokens

    def add_user_message(self, content: str) -> None:
        """把一条新指令追加进会话（L1 的 Critic 反馈走这条）。"""
        self._messages = [*self._messages, {"role": "user", "content": content}]

    async def advance(
        self,
        *,
        max_turns: int | None = None,
        cost_limit_usd: float | None = None,
    ) -> AgentStatus:
        """推进循环，直到某条停止条件成立；返回那条条件的自报状态。

        ``max_turns`` 是**本次**推进的轮数额度（L1 用它给 Critic 留够步数）；
        会话自身的 ``Budget.max_steps`` 是整条会话唯一的天花板，续跑接着算。
        ``cost_limit_usd`` 是本次推进可新增的成本额度；L1 在 Critic 花掉一部分
        全局预算后，用它把同一会话的下一段限制在剩余额度内。

        判定顺序与 §5.0 完全一致：先判完成，再判预算，再看步数。**不再执行工具**
        那条硬停语义保持原样——超出预算或额度的那一轮不落地。
        """
        schemas = self._runner._tools.list_schemas()
        max_steps = self.max_steps
        allowance = max_steps if max_turns is None else max(0, max_turns)
        turns = 0
        cost_at_start = self.usage.cost_usd
        phase_cost_limit = (
            max(0.0, self._budget.max_cost_usd - cost_at_start)
            if cost_limit_usd is None
            else max(0.0, cost_limit_usd)
        )

        while turns < allowance and self.turns < max_steps:
            index = self.turns
            self._last_index = index
            self.served_calls += 1
            try:
                result = await self._llm_turn(schemas)
            except Exception as exc:  # noqa: BLE001 - 基础设施故障就是 error 这条腿
                logger.warning("agent llm call failed: %s", exc)
                self.error = f"{type(exc).__name__}: {exc}"
                return "error"

            metrics = getattr(self._runner._llm, "last_metrics", None)
            self.usage = self.usage.add_call(metrics)
            reported = str((metrics or {}).get("model_used") or "").strip()
            if reported:
                self.model = reported

            # ① 终局：模型不再要求调工具。**先于预算判定**，理由见模块 docstring。
            if not result.tool_calls:
                self.finish_reason = str(getattr(result, "finish_reason", "") or "")
                self.reasoning_chars = int(
                    getattr(result, "reasoning_chars", 0) or 0
                )
                self.finish_content = str(result.content or "")
                if self.finish_reason == "length":
                    self.error = (
                        "LLM 输出被单次 completion 上限截断: "
                        f"finish_reason={self.finish_reason}, "
                        f"completion_tokens={int((metrics or {}).get('completion_tokens', 0) or 0)}, "
                        f"reasoning_chars={self.reasoning_chars}, "
                        f"content_chars={len(self.finish_content)}"
                    )
                    return "error"
                # 把模型这句"我为什么停"也留进历史：续跑（L1 的回边）时后一段必须
                # 看得见前一段说了什么，否则 Critic 的指令进来时上下文是断的。
                self._messages = [
                    *self._messages,
                    {"role": "assistant", "content": self.finish_content},
                ]
                return "completed"

            # ② 想让继续，但钱或 token 不够了。token 是兜底：主线判美元，它只防
            # 单个 agent 无节制地刷 token。
            if self.usage.cost_usd - cost_at_start >= phase_cost_limit or (
                self.max_tokens > 0 and self.usage.total_tokens >= self.max_tokens
            ):
                logger.info(
                    "budget exhausted phase_cost=%.6f limit=%.6f tokens=%d",
                    self.usage.cost_usd - cost_at_start,
                    phase_cost_limit,
                    self.usage.total_tokens,
                )
                return "budget_exhausted"

            # 自己拼 assistant 消息，不依赖 provider 的 messages 回传格式——
            # 两条执行路径（真 LLM / 脚本）因此走的是同一段历史维护代码。
            assistant: dict[str, Any] = {"role": "assistant", "content": result.content or ""}
            assistant["tool_calls"] = list(result.tool_calls)
            self._messages = [*self._messages, assistant]

            for call in result.tool_calls:
                step, tool_message = await self._runner.execute_call(
                    call, index=index, workspace=self._workspace
                )
                self.steps.append(step)
                self._messages = [*self._messages, tool_message]

            self._messages = compact_messages(self._messages, self._policy)
            turns += 1
            self.turns += 1

        return "step_limit"

    async def _llm_turn(self, schemas: list[dict[str, Any]]) -> Any:
        """要一轮决策，基础设施异常按 ``LLM_RETRY_ATTEMPTS`` 有界重试。

        重试预算内成功就当作这一轮正常返回；耗尽后把最后一次异常原样抛出，
        由调用方落 ``error``。``asyncio.CancelledError`` 不重试——取消会吞掉
        取消信号，必须原样上抛（与 ``app/agents/retry.py`` 同一口径）。
        """
        last: Exception | None = None
        for attempt in range(1, LLM_RETRY_ATTEMPTS + 1):
            try:
                return await self._runner._llm.chat_with_tools(self._messages, schemas)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 任何调用故障都可重试
                last = exc
                delay_ms = (
                    0
                    if attempt >= LLM_RETRY_ATTEMPTS
                    else LLM_RETRY_BACKOFF_MS[
                        min(attempt - 1, len(LLM_RETRY_BACKOFF_MS) - 1)
                    ]
                )
                status_code = _rate_limit_status(exc)
                self.usage = self.usage.add_retry(
                    LLMRetryEvent(
                        call_index=self.served_calls,
                        failed_attempt=attempt,
                        max_attempts=LLM_RETRY_ATTEMPTS,
                        delay_ms=delay_ms,
                        error_type=type(exc).__name__,
                        status_code=status_code,
                        rate_limited=status_code == 429,
                    )
                )
                if attempt >= LLM_RETRY_ATTEMPTS:
                    break
                logger.warning(
                    "agent llm call failed (attempt %d/%d): %s; retrying in %dms",
                    attempt,
                    LLM_RETRY_ATTEMPTS,
                    exc,
                    delay_ms,
                )
                await asyncio.sleep(delay_ms / 1000)
        assert last is not None  # 只有在至少失败过一次时才会走到这里
        raise last

    def build_run(
        self,
        status: AgentStatus,
        *,
        expert: str = "",
        review_rounds: int = 0,
        usage: Usage | None = None,
        stop_reason: str = "",
        error: str = "",
    ) -> AgentRun:
        """把会话的累计状态收成一个 ``AgentRun``（四个结局共用这一个出口）。"""
        if self.finish_content:
            observation = self.finish_content[:200]
        else:
            observation = ""
        if status == "completed":
            self.steps.append(
                Step(index=self._last_index, action="finish", observation=observation)
            )
        return AgentRun(
            status=status,
            steps=tuple(self.steps),
            usage=usage if usage is not None else self.usage,
            model=self.model,
            temperature=self._runner._temperature,
            expert=expert or self.expert,
            stop_reason=stop_reason,
            error=error or self.error,
            review_rounds=review_rounds,
        )


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

    def session(self, *, workspace: Path, goal: str, budget: Budget) -> LoopSession:
        """开一条可被续跑的会话。L1 需要拿着它跨回边继续推进。"""
        return LoopSession(
            self,
            workspace=workspace,
            goal=goal,
            budget=budget,
            instructions=self._instructions,
        )

    async def run(self, *, workspace: Path, goal: str, budget: Budget) -> AgentRun:
        session = self.session(workspace=workspace, goal=goal, budget=budget)
        status = await session.advance()
        return session.build_run(status, stop_reason=_stop_reason(status, session))

    async def execute_call(
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


def _stop_reason(status: AgentStatus, session: LoopSession) -> str:
    """四个结局各说清"我为什么停下"——CLI 与审计都读这一处。"""
    if status == "completed":
        return "模型声明完成"
    if status == "budget_exhausted":
        return (
            f"累计成本 ${session.usage.cost_usd:.6f} 已达上限 "
            f"${session.max_cost_usd:.6f}"
        )
    if status == "step_limit":
        return f"已达步数上限 {session.max_steps}"
    return "LLM 调用失败"


__all__ = ["LLMBackedAgent", "LoopSession"]
