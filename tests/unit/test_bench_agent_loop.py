"""§5.0 循环：四个结局，以及"完成优先于预算"这条顺序。"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from bench.agent.loop import LLMBackedAgent
from bench.agent.scripted import ScriptedLLM, ScriptedTurn
from bench.agent.tools import build_toolset
from bench.agent.types import Budget

FAILING_VERIFY = (sys.executable, "-c", "raise SystemExit(1)")


def make_agent(workspace: Path, turns: list[ScriptedTurn]) -> LLMBackedAgent:
    tools = build_toolset(workspace=workspace, verify_command=FAILING_VERIFY)
    return LLMBackedAgent(ScriptedLLM(turns), tools)


class _FallbackScriptedLLM(ScriptedLLM):
    """``config.model`` 说主模型，``last_metrics["model_used"]`` 说另一个——模拟
    fallback 链接手：真实作答的是后者，账本必须记后者。"""

    async def chat_with_tools(self, messages, tools, **kwargs):  # type: ignore[override]
        result = await super().chat_with_tools(messages, tools, **kwargs)
        self.last_metrics["model_used"] = "openrouter/fallback-model"
        return result


class _AlwaysFailingLLM(ScriptedLLM):
    """每次调用都抛——用来钉住"重试耗尽才落 error"，脚本用完不算失败。"""

    def __init__(self, *, calls: list[int] | None = None) -> None:
        super().__init__([], model="failing")
        self._calls = calls if calls is not None else []

    async def chat_with_tools(self, messages, tools, **kwargs):  # type: ignore[override]
        self._calls.append(1)
        raise RuntimeError("boom")


class _TruncatedLLM(ScriptedLLM):
    """输出额度耗尽：有 reasoning、有 token 账本，但最终 content 为空。"""

    async def chat_with_tools(self, messages, tools, **kwargs):  # type: ignore[override]
        result = await super().chat_with_tools(messages, tools, **kwargs)
        return replace(result, finish_reason="length", reasoning_chars=3620)


class _RateLimitedOnceLLM(ScriptedLLM):
    """首个真实调用被 429 拒绝，重试成功后必须留下可复核事件。"""

    def __init__(self) -> None:
        super().__init__([ScriptedTurn(final="重试后完成")], model="rate-limited")
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs):  # type: ignore[override]
        self.calls += 1
        if self.calls == 1:
            exc = RuntimeError("429 Too Many Requests")
            exc.status_code = 429  # type: ignore[attr-defined]
            raise exc
        return await super().chat_with_tools(messages, tools, **kwargs)


class _ContextWindowLLM(ScriptedLLM):
    def __init__(self) -> None:
        super().__init__([], model="context-limited")
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs):  # type: ignore[override]
        self.calls += 1
        exc = RuntimeError("request exceeds the available context size")
        exc.status_code = 400  # type: ignore[attr-defined]
        raise exc


@pytest.mark.asyncio
async def test_terminal_turn_is_completed(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, [ScriptedTurn(final="做完了")])
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.status == "completed"
    assert run.steps[-1].action == "finish"
    assert run.usage.llm_calls == 1


@pytest.mark.asyncio
async def test_running_out_of_steps_is_step_limit(tmp_path: Path) -> None:
    turns = [ScriptedTurn(tool="list_files", arguments={}) for _ in range(4)]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(10.0, 3))
    assert run.status == "step_limit"
    assert run.usage.llm_calls == 3


@pytest.mark.asyncio
async def test_cost_over_budget_is_budget_exhausted(tmp_path: Path) -> None:
    turns = [
        ScriptedTurn(tool="list_files", arguments={}, cost_usd=2.0),
        ScriptedTurn(final="本可以做完"),
    ]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 10))
    assert run.status == "budget_exhausted"
    assert run.usage.cost_usd == 2.0
    # 越额的那一轮没有执行工具：硬停就是不执行。
    assert run.steps == ()


@pytest.mark.asyncio
async def test_llm_failure_is_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 退避在测试里归零，钉的是语义不是墙钟。
    monkeypatch.setattr("bench.agent.loop.LLM_RETRY_BACKOFF_MS", (0, 0))
    llm = _AlwaysFailingLLM()
    tools = build_toolset(workspace=tmp_path, verify_command=FAILING_VERIFY)
    agent = LLMBackedAgent(llm, tools)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.status == "error"
    assert "boom" in run.error
    # 重试是有限的：尝试次数就是常量本身，不是无限重试。
    assert len(llm._calls) == 3


@pytest.mark.asyncio
async def test_deterministic_context_error_is_not_retried(tmp_path: Path) -> None:
    llm = _ContextWindowLLM()
    tools = build_toolset(workspace=tmp_path, verify_command=FAILING_VERIFY)

    run = await LLMBackedAgent(llm, tools).run(
        workspace=tmp_path,
        goal="目标",
        budget=Budget(1.0, 5),
    )

    assert run.status == "error"
    assert llm.calls == 1
    assert run.usage.retry_events == ()


@pytest.mark.asyncio
async def test_length_finish_is_error_with_output_budget_evidence(
    tmp_path: Path,
) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=FAILING_VERIFY)
    llm = _TruncatedLLM(
        [ScriptedTurn(final="", completion_tokens=1024)],
        model="reasoning-model",
    )

    run = await LLMBackedAgent(llm, tools).run(
        workspace=tmp_path,
        goal="目标",
        budget=Budget(1.0, 5),
    )

    assert run.status == "error"
    assert "finish_reason=length" in run.error
    assert "completion_tokens=1024" in run.error
    assert "reasoning_chars=3620" in run.error
    assert run.steps == ()


@pytest.mark.asyncio
async def test_transient_llm_error_is_retried_then_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次端点抖动不该把整条 run 判成 error——重试后接着走。"""
    monkeypatch.setattr("bench.agent.loop.LLM_RETRY_BACKOFF_MS", (0, 0))
    agent = make_agent(tmp_path, [ScriptedTurn(error="500"), ScriptedTurn(final="做完了")])
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.status == "completed"
    assert run.error == ""
    # 重试的那一轮没有落到轨迹上：失败不是一步。
    assert [step.action for step in run.steps] == ["finish"]
    assert len(run.usage.retry_events) == 1
    assert run.usage.retry_events[0].error_type == "RuntimeError"
    assert run.usage.retry_events[0].rate_limited is False


@pytest.mark.asyncio
async def test_rate_limit_retry_survives_into_successful_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bench.agent.loop.LLM_RETRY_BACKOFF_MS", (0, 0))
    tools = build_toolset(workspace=tmp_path, verify_command=FAILING_VERIFY)
    run = await LLMBackedAgent(_RateLimitedOnceLLM(), tools).run(
        workspace=tmp_path,
        goal="目标",
        budget=Budget(1.0, 5),
    )

    assert run.status == "completed"
    assert len(run.usage.retry_events) == 1
    event = run.usage.retry_events[0]
    assert event.call_index == 1
    assert event.failed_attempt == 1
    assert event.status_code == 429
    assert event.rate_limited is True


@pytest.mark.asyncio
async def test_rejected_tool_becomes_observation_and_loop_continues(
    tmp_path: Path,
) -> None:
    turns = [
        ScriptedTurn(tool="read_file", arguments={"path": "../outside.txt"}),
        ScriptedTurn(final="换个做法"),
    ]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.status == "completed"
    assert run.steps[0].outcome == "rejected"
    assert "工作区" in run.steps[0].observation


@pytest.mark.asyncio
async def test_unknown_tool_is_failed_not_rejected(tmp_path: Path) -> None:
    turns = [ScriptedTurn(tool="no_such_tool"), ScriptedTurn(final="好")]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.steps[0].outcome == "failed"


@pytest.mark.asyncio
async def test_completion_wins_over_budget_in_the_same_round(tmp_path: Path) -> None:
    """模型刚好在预算越顶的那一轮交了终局：它确实完成了，不能记成预算耗尽。"""
    turns = [
        ScriptedTurn(tool="list_files", arguments={}, cost_usd=0.4),
        ScriptedTurn(final="完成", cost_usd=0.9),
    ]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 10))
    assert run.status == "completed"
    assert run.usage.cost_usd > 1.0


@pytest.mark.asyncio
async def test_reported_model_is_the_one_that_answered(tmp_path: Path) -> None:
    """账本记的是**实际作答**的模型，不是配置里的主模型。"""
    tools = build_toolset(workspace=tmp_path, verify_command=FAILING_VERIFY)
    agent = LLMBackedAgent(
        _FallbackScriptedLLM([ScriptedTurn(final="完成")], model="primary-model"), tools
    )
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(1.0, 5))
    assert run.model == "openrouter/fallback-model"


@pytest.mark.asyncio
async def test_token_cap_is_a_budget_fallback(tmp_path: Path) -> None:
    """token 兜底命中同样归 budget_exhausted，且越额那一轮不执行工具。"""
    turns = [
        ScriptedTurn(
            tool="list_files",
            arguments={},
            prompt_tokens=80,
            completion_tokens=40,
        ),
        ScriptedTurn(final="本可以做完"),
    ]
    agent = make_agent(tmp_path, turns)
    run = await agent.run(
        workspace=tmp_path, goal="目标", budget=Budget(100.0, 10, max_tokens=100)
    )
    assert run.status == "budget_exhausted"
    assert run.usage.total_tokens == 120
    assert run.steps == ()
