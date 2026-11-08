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


@pytest.mark.asyncio
async def test_tool_loop_shows_cycle_line_in_message(tmp_path: Path) -> None:
    """Normal-budget run shows the cycle line with the right numbers in the
    tool message content, and the corresponding step observation in the result
    has NO cycle line (byte-identical evidence)."""
    import json
    from app.agents.memory import MemoryPolicy

    slot: dict[str, object] = {}
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
        max_tokens=100,
        memory_policy=MemoryPolicy(max_full_turns=2, max_observation_tokens=50),
        slot=slot,
    )

    assert result.status == "completed"
    # The tool message received by the model contains the cycle line.
    tool_messages = [
        str(m.get("content", ""))
        for m in llm.requests[-1]["messages"]
        if m.get("role") == "tool"
    ]
    assert tool_messages, "expected at least one tool message"
    cycle_msg = tool_messages[0]
    assert "[cycle 1/5 - tokens" in cycle_msg, f"missing cycle line in {cycle_msg!r}"
    # The step observation stored in the result is byte-identical, no cycle line.
    assert "[cycle" not in result.steps[0].observation, (
        f"step observation should not have cycle line but got {result.steps[0].observation!r}"
    )


@pytest.mark.asyncio
async def test_tool_loop_budget_nudge_when_over_ratio(tmp_path: Path) -> None:
    """With a tiny max_tokens that pushes usage over budget_nudge_ratio,
    the strong-warning sentence appears and slot loop_budget_nudges increments."""
    import json
    from app.agents.memory import MemoryPolicy

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

    slot: dict[str, object] = {}
    # max_tokens=20: first LLM call uses 15 tokens → 15/20 = 75% triggers nudge.
    # Second LLM call would push to 30 > 20, so status is budget_exhausted.
    # But the nudge was already added to the first tool observation.
    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=5,
        max_tokens=20,
        memory_policy=MemoryPolicy(max_full_turns=2, max_observation_tokens=50),
        slot=slot,
    )

    # The nudge should appear in the tool message sent to the model
    tool_messages = [
        str(m.get("content", ""))
        for m in llm.requests[-1]["messages"]
        if m.get("role") == "tool"
    ]
    assert tool_messages, "expected tool messages"
    has_warning = any("Budget running low" in msg for msg in tool_messages)
    assert has_warning, f"expected budget warning in tool messages: {tool_messages}"
    # slot loop_budget_nudges should be 1 (first tool observation had the warning)
    assert slot.get("loop_budget_nudges", 0) == 1, (
        f"expected loop_budget_nudges == 1, got {slot.get('loop_budget_nudges')}"
    )
    # The last LLM request should contain the strong-warning line.
    last_req = llm.requests[-1]
    all_messages = last_req["messages"]
    tool_messages = [
        m.get("content", "") for m in all_messages if m.get("role") == "tool"
    ]
    assert tool_messages, "expected tool messages"
    has_warning = any(
        "Budget running low" in content for content in tool_messages
    )
    assert has_warning, "expected strong-warning sentence in tool messages"
    # slot loop_budget_nudges should have been incremented.
    assert slot.get("loop_budget_nudges", 0) >= 1, (
        f"expected loop_budget_nudges >= 1, got {slot.get('loop_budget_nudges')}"
    )


@pytest.mark.asyncio
async def test_tool_loop_repeat_marker_on_identical_read(tmp_path: Path) -> None:
    """A second identical read_file call gets the repeat marker with #2 and
    slot loop_repeat_marks is 1, while a read with a different offset does
    not get the marker."""
    import json
    from app.agents.memory import MemoryPolicy

    tmp_path2 = tmp_path / "sub"
    tmp_path2.mkdir()

    # First read
    (tmp_path / "x.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path2 / "y.txt").write_text("world\n", encoding="utf-8")

    llm = FakeLLM(
        [
            [_call("1", "read_file", {"path": "x.txt"})],
            [_call("2", "read_file", {"path": "x.txt"})],  # identical → repeat
            [_call("3", "read_file", {"path": "x.txt"})],  # third, count goes up
            "finished",
        ]
    )
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    slot: dict[str, object] = {}
    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=10,
        max_tokens=100,
        memory_policy=MemoryPolicy(max_full_turns=2, max_observation_tokens=50, mark_repeated_reads=True),
        slot=slot,
    )

    assert result.status == "completed"
    # The second read should have the repeat marker.
    # With max_full_turns=2, only the last 2 turns are in the final request:
    # turn 2 (call 2 + result) and turn 3 (call 3 + result).
    # So tool_messages[0] is call 2 (repeat #2), tool_messages[1] is call 3 (repeat #3).
    tool_messages = [
        str(m.get("content", ""))
        for m in llm.requests[-1]["messages"]
        if m.get("role") == "tool"
    ]
    assert tool_messages, "expected tool messages"
    first_msg = tool_messages[0]  # this is the second call (repeat #2)
    assert "#2" in first_msg, f"expected repeat #2 in {first_msg!r}"
    # slot loop_repeat_marks should be 2 (second and third calls are marked)
    assert slot.get("loop_repeat_marks", 0) == 2, (
        f"expected loop_repeat_marks == 2, got {slot.get('loop_repeat_marks')}"
    )


@pytest.mark.asyncio
async def test_tool_loop_no_repeat_marker_different_offset(tmp_path: Path) -> None:
    """A read with a different offset does NOT get the repeat marker."""
    import json
    from app.agents.memory import MemoryPolicy

    (tmp_path / "x.txt").write_text("hello\n", encoding="utf-8")

    llm = FakeLLM(
        [
            [_call("1", "read_file", {"path": "x.txt", "offset": 0, "limit": 10})],
            [_call("2", "read_file", {"path": "x.txt", "offset": 5, "limit": 10})],  # different offset
            "finished",
        ]
    )
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    slot: dict[str, object] = {}
    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=10,
        max_tokens=100,
        memory_policy=MemoryPolicy(max_full_turns=2, max_observation_tokens=50, mark_repeated_reads=True),
        slot=slot,
    )

    assert result.status == "completed"
    tool_messages = [
        str(m.get("content", ""))
        for m in llm.requests[-1]["messages"]
        if m.get("role") == "tool"
    ]
    assert tool_messages, "expected tool messages"
    # No repeat marker should appear since offsets differ.
    combined = " ".join(tool_messages)
    assert "#2" not in combined, f"should not have repeat marker with different offset, got {combined!r}"
    assert slot.get("loop_repeat_marks", 0) == 0, (
        f"expected loop_repeat_marks == 0, got {slot.get('loop_repeat_marks')}"
    )


@pytest.mark.asyncio
async def test_tool_loop_no_repeat_marker_when_disabled(tmp_path: Path) -> None:
    """With policy.mark_repeated_reads False no marker appears."""
    import json
    from app.agents.memory import MemoryPolicy

    (tmp_path / "x.txt").write_text("hello\n", encoding="utf-8")

    llm = FakeLLM(
        [
            [_call("1", "read_file", {"path": "x.txt"})],
            [_call("2", "read_file", {"path": "x.txt"})],  # identical but disabled
            "finished",
        ]
    )
    tools = build_toolset(
        workspace=tmp_path,
        verify_command=(),
        names=("read_file",),
    )

    slot: dict[str, object] = {}
    result = await execute_tool_loop(
        llm=llm,
        messages=[{"role": "user", "content": "read"}],
        tools=tools,
        workspace=tmp_path,
        agent_name="test-implementer",
        max_steps=10,
        max_tokens=100,
        memory_policy=MemoryPolicy(max_full_turns=2, max_observation_tokens=50, mark_repeated_reads=False),
        slot=slot,
    )

    assert result.status == "completed"
    tool_messages = [
        str(m.get("content", ""))
        for m in llm.requests[-1]["messages"]
        if m.get("role") == "tool"
    ]
    assert tool_messages, "expected tool messages"
    combined = " ".join(tool_messages)
    assert "#2" not in combined, f"should not have repeat marker when disabled, got {combined!r}"
    assert slot.get("loop_repeat_marks", 0) == 0, (
        f"expected loop_repeat_marks == 0 when disabled, got {slot.get('loop_repeat_marks')}"
    )


@pytest.mark.asyncio
async def test_tool_loop_cycle_line_no_token_part_when_max_tokens_zero(tmp_path: Path) -> None:
    """With max_tokens 0 the cycle line carries no token part."""
    from app.agents.memory import MemoryPolicy

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
        max_tokens=0,  # no token budget
        memory_policy=MemoryPolicy(max_full_turns=2, max_observation_tokens=50),
    )

    assert result.status == "completed"
    tool_messages = [
        str(m.get("content", ""))
        for m in llm.requests[-1]["messages"]
        if m.get("role") == "tool"
    ]
    assert tool_messages, "expected tool messages"
    # Should be [cycle 1/5] only, no "/ tokens A/B" part.
    for msg in tool_messages:
        assert "[cycle 1/5]" in msg, f"expected '[cycle 1/5]' in message, got {msg!r}"
        assert "tokens" not in msg, f"should not have 'tokens' when max_tokens=0, got {msg!r}"
