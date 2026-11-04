from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.memory import MemoryPolicy
from app.agents.provider import ChatResult
from app.agents.stubs import MAX_COMPLETION_TOKENS, execute_tool_loop
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
        # 与真实 LLMClient 一致：assistant 消息必须带回 tool_calls，否则压缩
        # 逻辑会把它当成"结束轮"，tool 消息也会失去配对。
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": response,
        }
        return ChatResult(
            messages=[*messages, assistant],
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
async def test_tool_loop_caps_a_single_completion_request(tmp_path: Path) -> None:
    """总预算不是单次 completion 上限。

    实测记录里 1.2M 的 ``DELIVERY_MAX_TOKENS`` 被原样传给了 provider，第一次
    调用就报 "Max tokens must not exceed 1048576"，0 token 都没跑。
    """
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

    await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=5,
        max_tokens=1_200_000,
    )

    assert [request["max_tokens"] for request in llm.requests] == [
        MAX_COMPLETION_TOKENS,
        MAX_COMPLETION_TOKENS,
    ]


@pytest.mark.asyncio
async def test_tool_loop_compacts_old_turns_but_keeps_full_audit_steps(
    tmp_path: Path,
) -> None:
    """压缩只作用于发给模型的消息；Run Record 里的步骤观察保持完整。"""
    content = "x" * 1000 + "\n"
    (tmp_path / "x.txt").write_text(content, encoding="utf-8")
    llm = FakeLLM(
        [
            *[
                [_call(str(index), "read_file", {"path": "x.txt"})]
                for index in range(8)
            ],
            "finished",
        ]
    )
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read eight times"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=20,
        max_tokens=100_000,
        memory_policy=MemoryPolicy(max_full_turns=2, max_observation_tokens=50),
    )

    assert result.status == "completed"
    # 审计证据：每一步仍是工具返回的完整观察。
    assert len(result.steps[0].observation) > 1000
    # 模型侧：早期轮次已折叠成摘要，所有 tool 消息都被压到预算内。
    last_messages = llm.requests[-1]["messages"]
    assert any(
        message.get("role") == "system"
        and "早期 6 轮行动已省略" in str(message.get("content", ""))
        for message in last_messages
    )
    tool_contents = [
        str(message.get("content", ""))
        for message in last_messages
        if message.get("role") == "tool"
    ]
    assert tool_contents
    assert all(len(item) < 1000 for item in tool_contents)


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
async def test_tool_loop_does_not_accept_completion_over_the_budget(
    tmp_path: Path,
) -> None:
    class OverspendingFinalLLM(FakeLLM):
        async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
            result = await super().chat_with_tools(messages, tools, **kwargs)
            self.last_metrics = {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "cost_usd": 0.001,
                "llm_latency_ms": 2.0,
            }
            return result

    llm = OverspendingFinalLLM(["finished after overspending"])
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "finish"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=5,
        max_tokens=20,
    )

    assert result.status == "budget_exhausted"
    assert result.usage.total_tokens == 140
    assert result.steps == ()


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
