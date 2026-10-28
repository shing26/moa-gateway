from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.provider import ChatResult
from app.agents.stubs import execute_tool_loop
from app.agents.workspace_tools import build_toolset


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


class FakeLLM:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.last_metrics = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_usd": 0.001,
            "llm_latency_ms": 2.0,
        }

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
        self.requests.append({"messages": list(messages), **kwargs})
        # Simulate a provider that consumes more than the requested completion
        # allowance. The loop itself must still enforce the total budget.
        self.last_metrics = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_usd": 0.001,
            "llm_latency_ms": 2.0,
        }
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
async def test_tool_loop_returns_steps_and_usage(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            [_call("1", "write_file", {"path": "x.txt", "content": "ok"})],
            "finished",
        ]
    )
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=("python", "-c", "raise SystemExit(0)"),
        names=("read_file", "write_file", "search", "run_tests"),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[
            {"role": "system", "content": "implement"},
            {"role": "user", "content": "write x"},
        ],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=4,
        max_tokens=100,
    )

    assert result.status == "completed"
    assert result.content == "finished"
    assert result.usage.llm_calls == 2
    assert result.usage.prompt_tokens == 20
    assert result.usage.completion_tokens == 10
    assert [step.action for step in result.steps] == ["call_tool", "finish"]
    assert result.steps[0].tool == "write_file"
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "ok\n"


@pytest.mark.asyncio
async def test_tool_loop_stops_on_token_budget(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            [_call("1", "read_file", {"path": "x.txt"})],
            [_call("2", "read_file", {"path": "x.txt"})],
        ]
    )
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=5,
        max_tokens=20,
    )

    assert result.status == "budget_exhausted"
    assert result.usage.total_tokens == 30
    assert [request["max_tokens"] for request in llm.requests] == [20, 5]


@pytest.mark.asyncio
async def test_tool_loop_passes_only_the_remaining_token_budget(tmp_path: Path) -> None:
    llm = FakeLLM(
        [
            [_call("1", "read_file", {"path": "x.txt"})],
            "finished",
        ]
    )
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=5,
        max_tokens=40,
    )

    assert result.status == "completed"
    assert result.usage.total_tokens == 30
    # The second request gets the full remaining allowance; provider overrun is
    # detected after the call rather than assumed away by a stale prompt estimate.
    assert [request["max_tokens"] for request in llm.requests] == [40, 25]


@pytest.mark.asyncio
async def test_tool_loop_stops_when_provider_exceeds_the_budget(tmp_path: Path) -> None:
    class OverspendingLLM(FakeLLM):
        async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
            result = await super().chat_with_tools(messages, tools, **kwargs)
            self.last_metrics = {
                "prompt_tokens": 40,
                "completion_tokens": 30,
                "cost_usd": 0.001,
                "llm_latency_ms": 2.0,
            }
            return result

    llm = OverspendingLLM(
        [
            [_call("1", "read_file", {"path": "x.txt"})],
            "should not run",
        ]
    )
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=5,
        max_tokens=20,
    )

    assert result.status == "budget_exhausted"
    assert result.usage.total_tokens == 70
    assert len(llm.requests) == 1


@pytest.mark.asyncio
async def test_tool_loop_completes_when_final_text_arrives_after_max_steps(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            [_call("1", "read_file", {"path": "x.txt"})],
            [_call("2", "read_file", {"path": "x.txt"})],
            [_call("3", "read_file", {"path": "x.txt"})],
            "finished after three rounds",
        ]
    )
    (tmp_path / "x.txt").write_text("x\n", encoding="utf-8")
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read three times"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=3,
        max_tokens=100,
    )

    assert result.status == "completed"
    assert result.content == "finished after three rounds"
    assert result.stop_reason == "model finished"
    assert result.usage.llm_calls == 4
    assert [step.action for step in result.steps] == [
        "call_tool",
        "call_tool",
        "call_tool",
        "finish",
    ]


@pytest.mark.asyncio
async def test_parallel_tool_calls_stop_at_the_hard_step_budget(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            [
                _call("1", "write_file", {"path": "one.txt", "content": "one"}),
                _call("2", "write_file", {"path": "two.txt", "content": "two"}),
                _call("3", "write_file", {"path": "three.txt", "content": "three"}),
            ],
        ]
    )
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file", "write_file", "search"),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "write three files"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=2,
        max_tokens=100,
    )

    assert result.status == "step_limit"
    assert result.usage.llm_calls == 1
    assert [step.tool for step in result.steps] == ["write_file", "write_file"]
    assert "pending: write_file" in result.stop_reason
    assert (tmp_path / "one.txt").is_file()
    assert (tmp_path / "two.txt").is_file()
    assert not (tmp_path / "three.txt").exists()
