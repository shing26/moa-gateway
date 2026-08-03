from __future__ import annotations

import logging
import os
from typing import Any

from app.agents.contract import AgentEnvelope, SubAgent, register_agent
from app.agents.provider import LLMClient, LLMConfig

logger = logging.getLogger("moa.agents")


def _default_llm() -> LLMClient:
    return LLMClient(LLMConfig(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    ))


def _build_envelope_context(envelope: AgentEnvelope, role_tag: str) -> str:
    parts = [
        f"你是一个 {role_tag} 助手，需要根据用户输入生成高质量回复。",
        f"会话摘要: {envelope.global_summary}",
    ]
    if envelope.agent_local_slot:
        slot_lines = "\n".join(f"  {k}: {v}" for k, v in envelope.agent_local_slot.items())
        parts.append(f"本地上下文槽:\n{slot_lines}")
    return "\n\n".join(parts)


async def _execute_with_prompt(
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
    return await llm.chat(messages)


async def _execute_with_runtime_or_injected(
    llm: LLMClient | None,
    envelope: AgentEnvelope,
    role_tag: str,
    extra_instructions: str,
    agent_name: str,
) -> str:
    if llm is not None:
        return await _execute_with_prompt(llm, envelope, role_tag, extra_instructions, agent_name)
    async with _default_llm() as client:
        return await _execute_with_prompt(client, envelope, role_tag, extra_instructions, agent_name)


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
