from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.agents.contract import AgentEnvelope, SubAgent, register_agent
from app.agents.memory import MemoryPolicy, compact_messages, truncate_observation
from app.agents.provider import LLMClient, LLMConfig
from app.agents.tool_contract import call_tool
from app.agents.tools import tool_registry
from app.agents.workspace_tools import build_toolset, call_workspace_tool
from app.config import settings

logger = logging.getLogger("moa.agents")

#: 单次 completion 的输出上限。它和"整条 run 的 token 预算"是**两个量**：
#: 预算记的是所有调用的累计消耗，completion 上限约束的是这一次生成多长。
#: 把预算原样传给 provider 会撞上服务端输出上限（实测 1.2M 预算直接报
#: ``Max tokens must not exceed 1048576``，0 token 都没跑），也会诱发无意义的
#: 超长生成。真正的预算控制在下面按累计用量判定。
MAX_COMPLETION_TOKENS = 8_192


@dataclass(frozen=True)
class ToolLoopStep:
    index: int
    action: Literal["call_tool", "finish"]
    tool: str
    arguments: dict[str, Any]
    observation: str
    outcome: Literal["ok", "rejected", "failed", ""]
    elapsed_ms: float


@dataclass(frozen=True)
class ToolLoopUsage:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add_metrics(self, metrics: dict[str, Any] | None) -> "ToolLoopUsage":
        values = metrics or {}
        return ToolLoopUsage(
            llm_calls=self.llm_calls + 1,
            prompt_tokens=self.prompt_tokens
            + int(values.get("prompt_tokens", 0) or 0),
            completion_tokens=self.completion_tokens
            + int(values.get("completion_tokens", 0) or 0),
            cost_usd=self.cost_usd + float(values.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=self.llm_latency_ms
            + float(values.get("llm_latency_ms", 0.0) or 0.0),
        )


@dataclass(frozen=True)
class ToolLoopResult:
    status: Literal["completed", "step_limit", "budget_exhausted", "error"]
    content: str
    steps: tuple[ToolLoopStep, ...]
    usage: ToolLoopUsage
    stop_reason: str


class MockExpertLLM:
    """离线确定性 mock LLM，给专家池在 ``AGENT_LLM=mock`` 时使用。

    与 ``MockTaskLLM`` 不同：这个实现 ``LLMClient`` 协议
    （``chat`` / ``chat_with_tools``），返回基于输入的确定性文本，
    不发起网络调用。``last_metrics`` 恒为空字典——mock 不花钱，
    但 ``_execute_with_runtime_or_injected`` 的 ``capture_metrics``
    会读它，属性必须存在。

    为什么需要这个类：``CoderAgent`` / ``GeneralAgent`` / ``ReviewAgent``
    的 ``__init__`` 默认 ``llm=None``，此前 ``_default_llm()`` 无条件
    创建真实 ``LLMClient``，完全不看 ``settings.agent_llm``。结果是
    ``AGENT_LLM=mock``（默认）下专家仍发起真实调用，``LLM_API_KEY``
    为空时调用失败、被 ``expert_failure_reason()`` 认出后升级人工——
    与 ``AGENT_LLM=mock`` 的语义承诺矛盾。
    """

    def __init__(self) -> None:
        self.last_metrics: dict[str, Any] = {}

    async def __aenter__(self) -> "MockExpertLLM":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        last_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user = str(msg.get("content", ""))
                break
        system = str(messages[0].get("content", "")) if messages else ""
        if "编程" in system or "代码" in system:
            return f"已完成编码任务：{last_user[:200]}"
        if "审查" in system or "review" in system.lower():
            return f"已完成审查任务：{last_user[:200]}"
        return f"已完成：{last_user[:200]}"

    async def chat_with_tools(
        self, messages: list[dict], tools: list[dict], **kwargs: Any
    ) -> Any:
        from app.agents.provider import ChatResult

        content = await self.chat(messages)
        return ChatResult(
            messages=messages + [{"role": "assistant", "content": content}],
            content=content,
            tool_calls=[],
        )


def _default_llm() -> LLMClient:
    # 专家池也要遵守 AGENT_LLM 的语义承诺：mock 时不建真实客户端。
    # 此前这里无条件创建 LLMClient，导致默认配置下协作链路必然失败。
    if settings.agent_llm == "mock":
        return MockExpertLLM()  # type: ignore[return-value]
    return LLMClient(LLMConfig.from_env("LLM"))


def _build_envelope_context(envelope: AgentEnvelope, role_tag: str) -> str:
    parts = [
        f"你是一个 {role_tag} 助手，需要根据用户输入生成高质量回复。",
        f"会话摘要: {envelope.global_summary}",
    ]
    if envelope.agent_local_slot:
        slot_lines = "\n".join(f"  {k}: {v}" for k, v in envelope.agent_local_slot.items())
        parts.append(f"本地上下文槽:\n{slot_lines}")
    return "\n\n".join(parts)


def _tool_runtime(envelope: AgentEnvelope):
    """选择本次专家可用的工具集。

    只有显式带有 ``workspace_path`` 的请求才切到工作区工具。这样普通业务
    专家仍使用全局工具注册表，而协作/实验台可以在同一套 Agent 实现上获得
    文件读写、搜索和固定命令验证能力。
    """
    raw_workspace = (envelope.agent_local_slot or {}).get("workspace_path")
    if not raw_workspace:
        return tool_registry, None

    workspace = Path(str(raw_workspace)).resolve()
    if not workspace.is_dir():
        raise ValueError(f"工作区不存在或不是目录: {workspace}")
    raw_verify = (envelope.agent_local_slot or {}).get("verify_command")
    verify_command = (
        tuple(str(token) for token in raw_verify)
        if isinstance(raw_verify, (list, tuple))
        else ()
    )
    names = [
        "read_file",
        "write_file",
        "replace_in_file",
        "list_files",
        "search",
        "git_diff",
    ]
    if verify_command:
        names.append("run_tests")
    return (
        build_toolset(
            workspace=workspace,
            verify_command=verify_command,
            names=tuple(names),
        ),
        workspace,
    )


async def execute_tool_loop(
    *,
    llm: Any,
    messages: list[dict[str, Any]],
    tools: Any,
    workspace: Path | None,
    agent_name: str,
    max_steps: int,
    max_tokens: int = 0,
    slot: dict[str, object] | None = None,
    memory_policy: MemoryPolicy | None = None,
) -> ToolLoopResult:
    import time

    slot = slot if slot is not None else {}
    policy = memory_policy or MemoryPolicy()
    logger.info("%s structured tool loop", agent_name)
    chat_with_tools = getattr(llm, "chat_with_tools", None)
    if chat_with_tools is None:
        content = await llm.chat(messages)
        return ToolLoopResult(
            status="completed",
            content=content,
            steps=(
                ToolLoopStep(
                    index=1,
                    action="finish",
                    tool="",
                    arguments={},
                    observation=content[:200],
                    outcome="",
                    elapsed_ms=0.0,
                ),
            ),
            usage=ToolLoopUsage(),
            stop_reason="model finished",
        )

    schemas = tools.list_schemas()
    steps: list[ToolLoopStep] = []
    usage = ToolLoopUsage()
    current_messages = list(messages)
    executed_tool_calls = 0
    # Repeat tracking: key -> count
    repeat_counts: dict[tuple, int] = {}
    # Slot counters
    slot.setdefault("loop_repeat_marks", 0)
    slot.setdefault("loop_budget_nudges", 0)
    def request_tokens() -> int | None:
        """本次调用的 completion 上限，**不是**剩余总预算。

        预算是累计量，只能事后结算（调用前无从得知 prompt 会占多少）；把它原样
        当 completion 上限传给 provider，既语义错误（预算 40 万 ≠ 这一次要生成
        40 万），也会撞服务端上限。这里只保证单次不超 ``MAX_COMPLETION_TOKENS``，
        总预算仍由循环在每次调用后按 ``usage.total_tokens`` 判定。
        """
        if max_tokens <= 0:
            # 未设总预算时沿用 provider 配置的单次上限（旧行为），不要用一个
            # 硬编码值覆盖模型自身的 max_tokens。
            return None
        remaining = max(0, max_tokens - usage.total_tokens)
        if remaining <= 0:
            return 0
        return min(remaining, MAX_COMPLETION_TOKENS)

    def record_usage() -> None:
        nonlocal usage
        metrics = getattr(llm, "last_metrics", None) or {}
        usage = usage.add_metrics(metrics)

    try:
        result = await chat_with_tools(
            current_messages,
            schemas,
            max_tokens=request_tokens(),
        )
        record_usage()
        while True:
            if max_tokens > 0 and usage.total_tokens > max_tokens:
                return ToolLoopResult(
                    status="budget_exhausted",
                    content=str(result.content or ""),
                    steps=tuple(steps),
                    usage=usage,
                    stop_reason=(
                        f"token budget exceeded: {usage.total_tokens} > {max_tokens}"
                    ),
                )
            if not result.tool_calls:
                steps.append(
                    ToolLoopStep(
                        index=len(steps) + 1,
                        action="finish",
                        tool="",
                        arguments={},
                        observation=str(result.content or "")[:200],
                        outcome="",
                        elapsed_ms=0.0,
                    )
                )
                return ToolLoopResult(
                    status="completed",
                    content=str(result.content or ""),
                    steps=tuple(steps),
                    usage=usage,
                    stop_reason="model finished",
                )

            if max_tokens > 0 and usage.total_tokens >= max_tokens:
                return ToolLoopResult(
                    status="budget_exhausted",
                    content=str(result.content or ""),
                    steps=tuple(steps),
                    usage=usage,
                    stop_reason=(
                        f"token budget exhausted: {usage.total_tokens} >= {max_tokens}"
                    ),
                )

            calls = list(result.tool_calls)
            for call_index, call in enumerate(calls):
                if executed_tool_calls >= max_steps:
                    pending = ", ".join(
                        str((call.get("function") or {}).get("name", ""))
                        for call in calls[call_index:]
                    )
                    return ToolLoopResult(
                        status="step_limit",
                        content=str(result.content or ""),
                        steps=tuple(steps),
                        usage=usage,
                        stop_reason=(
                            "tool step limit reached; pending: " + pending
                        ),
                    )
                started = time.monotonic()
                name = str((call.get("function") or {}).get("name", ""))
                arguments: dict[str, Any] = {}
                slot["tool_calls_total"] = int(slot.get("tool_calls_total", 0) or 0) + 1
                tool = tools.get(name)
                outcome_kind: Literal["ok", "rejected", "failed", ""] = "failed"
                if tool is None:
                    observation = '{"error": "unknown tool"}'
                    slot["tool_errors_total"] = int(
                        slot.get("tool_errors_total", 0) or 0
                    ) + 1
                else:
                    try:
                        arguments = json.loads(
                            (call.get("function") or {}).get("arguments") or "{}"
                        )
                    except json.JSONDecodeError as exc:
                        observation = f'{{"error": "参数不是合法 JSON: {exc}"}}'
                        outcome_kind = "rejected"
                        slot["tool_arg_rejections_total"] = int(
                            slot.get("tool_arg_rejections_total", 0) or 0
                        ) + 1
                    else:
                        outcome = await (
                            call_workspace_tool(
                                tool,
                                arguments,
                                workspace=workspace,
                            )
                            if workspace is not None
                            else call_tool(
                                tool,
                                arguments,
                                session_id=str(slot.get("session_id", "")),
                            )
                        )
                        outcome_kind = outcome.kind
                        if outcome.kind == "ok":
                            observation = outcome.detail
                        elif outcome.kind == "rejected":
                            observation = f'{{"error": "参数被拒：{outcome.detail}"}}'
                            slot["tool_arg_rejections_total"] = int(
                                slot.get("tool_arg_rejections_total", 0) or 0
                            ) + 1
                        else:
                            observation = f'{{"error": "{outcome.detail}"}}'
                            slot["tool_errors_total"] = int(
                                slot.get("tool_errors_total", 0) or 0
                            ) + 1
                steps.append(
                    ToolLoopStep(
                        index=len(steps) + 1,
                        action="call_tool",
                        tool=name,
                        arguments=(
                            arguments if isinstance(arguments, dict) else {}
                        ),
                        observation=observation,
                        outcome=outcome_kind,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                )
                # 完整观察留在 steps（Run Record 的审计证据）；回给模型的那份
                # 走上下文预算。命令输出保留尾部，文件读取保留头部。
                model_observation = truncate_observation(
                    observation,
                    policy,
                    keep_tail=name == "run_tests",
                )
                # ---- repeat marker (read_file / search) ----
                if policy.mark_repeated_reads:
                    # parse key: tool name + args tuple
                    try:
                        if name == "read_file":
                            args = arguments
                            key = ("read_file", (args.get("path"), args.get("offset"), args.get("limit")))
                        elif name == "search":
                            args = arguments
                            key = ("search", (args.get("path"), args.get("pattern")))
                        else:
                            key = None
                        if key is not None:
                            count = repeat_counts.get(key, 0) + 1
                            repeat_counts[key] = count
                            if count >= 2:
                                repeat_line = f"[repeat #{count} of this exact call - the content is already in your context; do not read it again]"
                                model_observation = repeat_line + "\n" + model_observation
                                slot["loop_repeat_marks"] = int(slot.get("loop_repeat_marks", 0)) + 1
                    except (json.JSONDecodeError, TypeError):
                        # skip marker if arguments failed to parse as JSON
                        pass
                # ---- budget line ----
                executed_tool_calls += 1
                A = usage.total_tokens
                B = max_tokens
                if B > 0:
                    P = A * 100 // B
                    cycle_line = f"[cycle {executed_tool_calls}/{max_steps} - tokens {A}/{B} - {P} percent used]"
                    if policy.budget_nudge_ratio and P >= policy.budget_nudge_ratio * 100:
                        cycle_line += " Budget running low: stop reading, move to editing and verification now."
                    model_observation = model_observation + "\n" + cycle_line
                    if policy.budget_nudge_ratio and P >= policy.budget_nudge_ratio * 100:
                        slot["loop_budget_nudges"] = int(slot.get("loop_budget_nudges", 0)) + 1
                else:
                    cycle_line = f"[cycle {executed_tool_calls}/{max_steps}]"
                    model_observation = model_observation + "\n" + cycle_line
                result.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": model_observation,
                    }
                )
            # 同一条会话推进多轮后，早期轮次只留一行摘要。没有这一步，40 步
            # 的探索会把整段历史重复送进每一次调用——实测 17 次调用烧掉 101 万
            # prompt token，却还没开始写文件。
            current_messages = compact_messages(result.messages, policy)
            allowance = request_tokens()
            if max_tokens > 0 and allowance is not None and allowance <= 0:
                return ToolLoopResult(
                    status="budget_exhausted",
                    content=str(result.content or ""),
                    steps=tuple(steps),
                    usage=usage,
                    stop_reason=(
                        f"token budget exhausted: {usage.total_tokens} >= {max_tokens}"
                    ),
                )
            result = await chat_with_tools(
                current_messages,
                schemas,
                max_tokens=allowance,
            )
            record_usage()
    except Exception as exc:
        return ToolLoopResult(
            status="error",
            content="",
            steps=tuple(steps),
            usage=usage,
            stop_reason=f"{type(exc).__name__}: {exc}",
        )


async def _execute_with_tools(
    llm: LLMClient,
    envelope: AgentEnvelope,
    role_tag: str,
    extra_instructions: str,
    agent_name: str,
) -> str:
    override = (envelope.agent_local_slot or {}).get("system_prompt", "")
    system = (
        str(override)
        if override
        else _build_envelope_context(envelope, role_tag) + extra_instructions
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(envelope.history or ())
    messages.append({"role": "user", "content": envelope.user_raw_input})
    tools, workspace = _tool_runtime(envelope)
    slot = dict(envelope.agent_local_slot or {})
    slot["session_id"] = envelope.session_id
    result = await execute_tool_loop(
        llm=llm,
        messages=messages,
        tools=tools,
        workspace=workspace,
        agent_name=agent_name,
        max_steps=settings.agent_max_steps,
        slot=slot,
    )
    envelope.agent_local_slot.update(slot)
    if result.status == "error":
        raise RuntimeError(result.stop_reason)
    if result.status == "step_limit":
        return result.content + f"\n[工具调用已达上限] {result.stop_reason}"
    return result.content


async def _execute_with_runtime_or_injected(
    llm: LLMClient | None,
    envelope: AgentEnvelope,
    role_tag: str,
    extra_instructions: str,
    agent_name: str,
) -> str:
    def capture_metrics(client: LLMClient) -> None:
        metrics = getattr(client, "last_metrics", None)
        if metrics:
            envelope.agent_local_slot["llm_metrics"] = metrics

    if llm is not None:
        output = await _execute_with_tools(llm, envelope, role_tag, extra_instructions, agent_name)
        capture_metrics(llm)
        return output
    async with _default_llm() as client:
        output = await _execute_with_tools(client, envelope, role_tag, extra_instructions, agent_name)
        capture_metrics(client)
        return output


class CoderAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        return await _execute_with_runtime_or_injected(
            self._llm, envelope,
            role_tag="编程与代码",
            extra_instructions=(
                "\n\n你是一个专业的编码助手。当你输出代码时，请确保:"
                "\n- 代码正确、可运行、包含必要注释"
                "\n- 优先使用 Python 3.12+ 特性"
                "\n- 如果输出 JSON，确保是合法 JSON"
                "\n- 不要包含 TODO/FIXME 标记"
            ),
            agent_name="coder_agent",
        )


class GeneralAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        return await _execute_with_runtime_or_injected(
            self._llm, envelope,
            role_tag="通用问答与推理",
            extra_instructions=(
                "\n\n你是一个通用助手。请遵循:"
                "\n- 回答准确、简洁、有条理"
                "\n- 如果涉及代码分析，请附上关键代码片段"
                "\n- 如果输出 JSON，确保是合法 JSON"
                "\n- 不要包含 TODO/FIXME 标记"
            ),
            agent_name="general_agent",
        )


register_agent("coder", CoderAgent())
register_agent("general", GeneralAgent())
