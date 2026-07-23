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


class CoderAgent:
    """Sub-agent focused on code generation and programming tasks."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or _default_llm()

    async def execute(self, envelope: AgentEnvelope) -> str:
        system = _build_envelope_context(envelope, "编程与代码")
        system += (
            "\n\n你是一个专业的编码助手。当你输出代码时，请确保:"
            "\n- 代码正确、可运行、包含必要注释"
            "\n- 优先使用 Python 3.12+ 特性"
            "\n- 如果输出 JSON，确保是合法 JSON"
            "\n- 不要包含 TODO/FIXME 标记"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": envelope.user_raw_input},
        ]
        logger.info("coder_agent execute trace=%s session=%s", envelope.trace_id, envelope.session_id)
        return await self.llm.chat(messages)


class GeneralAgent:
    """Sub-agent for general-purpose Q&A, reasoning, and summarization."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or _default_llm()

    async def execute(self, envelope: AgentEnvelope) -> str:
        system = _build_envelope_context(envelope, "通用问答与推理")
        system += (
            "\n\n你是一个通用助手。请遵循:"
            "\n- 回答准确、简洁、有条理"
            "\n- 如果涉及代码分析，请附上关键代码片段"
            "\n- 如果输出 JSON，确保是合法 JSON"
            "\n- 不要包含 TODO/FIXME 标记"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": envelope.user_raw_input},
        ]
        logger.info("general_agent execute trace=%s session=%s", envelope.trace_id, envelope.session_id)
        return await self.llm.chat(messages)


# Register default instances so AGENT_REGISTRY works without manual wiring.
register_agent("coder", CoderAgent())
register_agent("general", GeneralAgent())
