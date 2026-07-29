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


class SubAgent(Protocol):
    async def execute(self, envelope: AgentEnvelope) -> str: ...


AGENT_REGISTRY: dict[str, SubAgent] = {}


def register_agent(name: str, agent: SubAgent) -> None:
    AGENT_REGISTRY[name] = agent


def get_agent(name: str) -> SubAgent | None:
    return AGENT_REGISTRY.get(name)
