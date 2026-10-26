from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.provider import ChatResult
from apps.code_review_pipeline.delivery.adapters.production_agent import (
    ProductionImplementer,
)


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


class FakeImplementerLLM:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []
        self.last_metrics = {
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "cost_usd": 0.002,
            "llm_latency_ms": 12.0,
        }
        self._responses = [
            [
                _call(
                    "1",
                    "write_file",
                    {"path": "app.py", "content": "def add(a, b):\n    return a + b\n"},
                )
            ],
            "implemented the fix",
        ]

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
        self.messages.append([dict(item) for item in messages])
        response = self._responses.pop(0)
        if isinstance(response, str):
            return ChatResult(
                messages=[*messages, {"role": "assistant", "content": response}],
                content=response,
                tool_calls=[],
            )
        return ChatResult(
            messages=[*messages, {"role": "assistant", "content": None}],
            content="",
            tool_calls=response,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_implementer_returns_structured_attempt_and_writes_file(
    tmp_path: Path,
) -> None:
    llm = FakeImplementerLLM()
    adapter = ProductionImplementer(
        llm=llm,
        verify_command=("python", "-m", "pytest", "-q"),
    )

    attempt = await adapter.run(
        workspace=tmp_path,
        goal="fix add",
        feedback="the previous version returned a - b",
        max_steps=5,
        max_tokens=1000,
    )

    assert attempt.role == "implementer"
    assert attempt.status == "completed"
    assert attempt.summary == "implemented the fix"
    assert attempt.usage.llm_calls == 2
    assert attempt.usage.prompt_tokens == 200
    assert attempt.usage.completion_tokens == 50
    assert attempt.steps[0].tool == "write_file"
    assert "return a + b" in (tmp_path / "app.py").read_text(encoding="utf-8")
    assert "the previous version returned a - b" in "\n".join(
        str(item["content"])
        for item in llm.messages[0]
        if item.get("role") == "user"
    )


@pytest.mark.asyncio
async def test_implementer_has_no_write_tool_when_not_requested_by_registry(
    tmp_path: Path,
) -> None:
    class UnknownToolLLM(FakeImplementerLLM):
        def __init__(self) -> None:
            super().__init__()
            self._responses = [
                [_call("1", "git_push", {"branch": "main"})],
                "stopped without using GitHub",
            ]

    adapter = ProductionImplementer(
        llm=UnknownToolLLM(),
        verify_command=("python", "-c", "raise SystemExit(0)"),
    )

    attempt = await adapter.run(
        workspace=tmp_path,
        goal="fix",
        feedback="",
        max_steps=3,
        max_tokens=1000,
    )

    assert attempt.steps[0].outcome == "failed"
    assert "unknown tool" in attempt.steps[0].observation
