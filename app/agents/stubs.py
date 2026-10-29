from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.agents.contract import AgentEnvelope, SubAgent, register_agent
from app.agents.provider import LLMClient, LLMConfig
from app.agents.tool_contract import call_tool
from app.agents.tools import tool_registry
from app.agents.workspace_tools import build_toolset, call_workspace_tool
from app.config import settings

logger = logging.getLogger("moa.agents")


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
    names = ["read_file", "write_file", "list_files", "search"]
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


async def _execute_with_tools(
    llm: LLMClient,
    envelope: AgentEnvelope,
    role_tag: str,
    extra_instructions: str,
    agent_name: str,
) -> str:
    override = (envelope.agent_local_slot or {}).get("system_prompt", "")
    if override:
        system = override
    else:
        system = _build_envelope_context(envelope, role_tag)
        system += extra_instructions
    history = list(envelope.history) if envelope.history else []
    messages = [
        {"role": "system", "content": system},
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": envelope.user_raw_input})
    logger.info("%s execute trace=%s session=%s", agent_name, envelope.trace_id, envelope.session_id)
    tools, workspace = _tool_runtime(envelope)
    chat_with_tools = getattr(llm, "chat_with_tools", None)
    if chat_with_tools is None:
        return await llm.chat(messages)
    schemas = tools.list_schemas()
    result = await chat_with_tools(messages, schemas)
    for _ in range(settings.agent_max_steps):
        if not result.tool_calls:
            break
        for call in result.tool_calls:
            slot = envelope.agent_local_slot
            slot["tool_calls_total"] = int(slot.get("tool_calls_total", 0) or 0) + 1
            tool = tools.get(call["function"]["name"])
            if tool is None:
                output = '{"error": "unknown tool"}'
                slot["tool_errors_total"] = int(slot.get("tool_errors_total", 0) or 0) + 1
            else:
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    output = f'{{"error": "参数不是合法 JSON: {exc}"}}'
                    slot["tool_arg_rejections_total"] = int(
                        slot.get("tool_arg_rejections_total", 0) or 0
                    ) + 1
                else:
                    # 与 ReActLoop 共用同一份契约（ADR-018）：注入 session_id（覆盖式，
                    # 模型自带的值作废）→ 校验 → 执行。此前这条路径既不注入也不校验，
                    # 模型带上 session_id 就能直达 handler。
                    outcome = await (
                        call_workspace_tool(tool, arguments, workspace=workspace)
                        if workspace is not None
                        else call_tool(
                            tool, arguments, session_id=envelope.session_id
                        )
                    )
                    if outcome.kind == "ok":
                        output = outcome.detail
                    elif outcome.kind == "rejected":
                        output = f'{{"error": "参数被拒：{outcome.detail}"}}'
                        slot["tool_arg_rejections_total"] = int(
                            slot.get("tool_arg_rejections_total", 0) or 0
                        ) + 1
                    else:
                        output = f'{{"error": "{outcome.detail}"}}'
                        slot["tool_errors_total"] = int(
                            slot.get("tool_errors_total", 0) or 0
                        ) + 1
            result.messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": output}
            )
        result = await chat_with_tools(result.messages, schemas)
    if result.tool_calls:
        pending = ", ".join(call["function"]["name"] for call in result.tool_calls)
        return result.content + f"\n[工具调用已达上限，待处理工具: {pending}]"
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
