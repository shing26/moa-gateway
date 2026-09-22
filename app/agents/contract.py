from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentEnvelope:
    trace_id: str
    session_id: str
    user_raw_input: str
    global_summary: str
    agent_local_slot: dict[str, object]
    history: tuple = ()
    # 重试归因：由 app/agents/retry.py 在重试前用 dataclasses.replace 填入。
    # 首次尝试恒为 (0, "")，所以既有构造点不受影响。agent 只应把它拼进自己的
    # prompt，不要改写 user_raw_input——那是审计与记忆的原始输入。
    retry_attempt: int = 0
    failure_reason: str = ""


class SubAgent(Protocol):
    async def execute(self, envelope: AgentEnvelope) -> str: ...


AGENT_REGISTRY: dict[str, SubAgent] = {}


def register_agent(name: str, agent: SubAgent) -> None:
    AGENT_REGISTRY[name] = agent


def get_agent(name: str) -> SubAgent | None:
    return AGENT_REGISTRY.get(name)
