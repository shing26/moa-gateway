from __future__ import annotations

from app.agents.contract import AgentEnvelope, SubAgent

class CoderAgent:
    async def execute(self, envelope: AgentEnvelope) -> str:
        return f"[stub coder] {envelope.user_raw_input}"

class GeneralAgent:
    async def execute(self, envelope: AgentEnvelope) -> str:
        return f"[stub general] {envelope.user_raw_input}"
