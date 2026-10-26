from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.stubs import execute_tool_loop
from app.agents.workspace_tools import build_toolset
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    AgentStep,
    DeliveryUsage,
)

_IMPLEMENTER_SYSTEM = """\
You are the Implementer in a governed repository delivery pipeline.
Modify only the provided workspace. Use the available file and test tools.
You cannot access GitHub, create approvals, push, or merge.
Read before editing. When the goal is complete, stop calling tools and summarize.
"""


class ProductionImplementer:
    def __init__(
        self,
        *,
        llm: Any,
        verify_command: tuple[str, ...],
        system_prompt: str = _IMPLEMENTER_SYSTEM,
    ) -> None:
        self._llm = llm
        self._verify_command = tuple(verify_command)
        self._system_prompt = system_prompt

    async def run(
        self,
        *,
        workspace: Path,
        goal: str,
        feedback: str,
        max_steps: int,
        max_tokens: int,
    ) -> AgentAttempt:
        root = Path(workspace).resolve()
        names = ["read_file", "write_file", "search"]
        if self._verify_command:
            names.append("run_tests")
        tools = build_toolset(
            workspace=root,
            verify_command=self._verify_command,
            names=tuple(names),
        )
        user_parts = [f"Goal:\n{goal}"]
        if feedback.strip():
            user_parts.append(f"Required revision:\n{feedback.strip()}")
        result = await execute_tool_loop(
            llm=self._llm,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            tools=tools,
            workspace=root,
            agent_name="delivery_implementer",
            max_steps=max_steps,
            max_tokens=max_tokens,
        )
        return AgentAttempt(
            role="implementer",
            status=result.status,
            summary=result.content.strip()[:1000],
            stop_reason=result.stop_reason,
            usage=DeliveryUsage(
                llm_calls=result.usage.llm_calls,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cost_usd=result.usage.cost_usd,
                llm_latency_ms=result.usage.llm_latency_ms,
            ),
            steps=tuple(
                AgentStep(
                    index=step.index,
                    action=step.action,
                    tool=step.tool,
                    arguments=dict(step.arguments),
                    observation=step.observation,
                    outcome=step.outcome,
                    elapsed_ms=step.elapsed_ms,
                )
                for step in result.steps
            ),
        )
