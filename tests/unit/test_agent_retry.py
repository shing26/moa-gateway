"""执行期重试（ADR-010）：带归因重试、预算耗尽升级、跨尝试成本累加。

重试预算不是配置项而是状态机的结构事实，所以这里有一条漂移守卫把
``RETRY_BUDGET`` 与转移表钉在一起——改任一侧都会红。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.contract import AgentEnvelope
from app.agents.retry import RETRY_BUDGET, AgentExecutionFailed, execute_with_retry
from app.fsm.state_machine import Event, State, next_state


def make_envelope(**overrides) -> AgentEnvelope:
    base: dict = {
        "trace_id": "t-1",
        "session_id": "s-1",
        "user_raw_input": "原始请求",
        "global_summary": "",
        "agent_local_slot": {},
    }
    base.update(overrides)
    return AgentEnvelope(**base)


class FlakyAgent:
    """前 ``failures`` 次抛异常，之后返回 ``reply``。"""

    def __init__(self, failures: int, reply: str = "ok", metrics: dict | None = None) -> None:
        self.failures = failures
        self.reply = reply
        self.metrics = metrics
        self.calls = 0
        self.envelopes: list[AgentEnvelope] = []

    async def execute(self, envelope: AgentEnvelope) -> str:
        self.calls += 1
        self.envelopes.append(envelope)
        if self.metrics is not None:
            envelope.agent_local_slot["llm_metrics"] = dict(self.metrics)
        if self.calls <= self.failures:
            raise RuntimeError(f"boom-{self.calls}")
        return self.reply


@pytest.mark.asyncio
async def test_retry_succeeds_and_feeds_failure_reason_back():
    agent = FlakyAgent(failures=1)
    envelope = make_envelope()

    output, attempts, _ = await execute_with_retry(agent, envelope, backoff_ms=0)

    assert output == "ok"
    assert attempts == 2
    assert agent.calls == 2
    # 首次尝试不带归因
    assert agent.envelopes[0].retry_attempt == 0
    assert agent.envelopes[0].failure_reason == ""
    # 归因喂回：第二次尝试的 envelope 带上失败原因与尝试序号
    assert agent.envelopes[1].retry_attempt == 1
    assert "boom-1" in agent.envelopes[1].failure_reason
    # user_raw_input 必须保持用户原话：它是审计与长期记忆的原始输入
    assert agent.envelopes[1].user_raw_input == "原始请求"


@pytest.mark.asyncio
async def test_retry_reuses_the_same_agent_local_slot():
    """跨尝试共享同一个可变槽位，否则 agent 写下的 plan/task_results 会丢。"""
    agent = FlakyAgent(failures=1)
    envelope = make_envelope()

    await execute_with_retry(agent, envelope, backoff_ms=0)

    assert agent.envelopes[0].agent_local_slot is agent.envelopes[1].agent_local_slot


@pytest.mark.asyncio
async def test_metrics_accumulate_across_attempts():
    """失败尝试烧掉的 token/成本不能丢：审计里的成本必须是所有尝试之和。"""
    agent = FlakyAgent(failures=1, metrics={"cost_usd": 0.25, "prompt_tokens": 10})
    envelope = make_envelope()

    await execute_with_retry(agent, envelope, backoff_ms=0)

    metrics = envelope.agent_local_slot["llm_metrics"]
    assert metrics["cost_usd"] == pytest.approx(0.5)
    assert metrics["prompt_tokens"] == 20


@pytest.mark.asyncio
async def test_budget_exhausted_raises_with_attempts_and_last_error():
    agent = FlakyAgent(failures=99)
    envelope = make_envelope()

    with pytest.raises(AgentExecutionFailed) as excinfo:
        await execute_with_retry(agent, envelope, backoff_ms=0)

    assert excinfo.value.attempts == RETRY_BUDGET + 1
    assert isinstance(excinfo.value.last_error, RuntimeError)
    assert agent.calls == RETRY_BUDGET + 1


@pytest.mark.asyncio
async def test_on_failure_callback_walks_the_fsm_into_suspended():
    """重试耗尽时状态机序列必须真的落到 SUSPENDED——表就是预算。"""
    agent = FlakyAgent(failures=99)
    # 真实序列：ROUTED --TASK_STARTED--> EXECUTING，之后每次失败发 TASK_FAILED
    state = next_state(State.ROUTED, Event.TASK_STARTED)
    attempts_seen: list[int] = []

    async def on_failure(attempt: int, exc: BaseException) -> None:
        nonlocal state
        state = next_state(state, Event.TASK_FAILED)
        attempts_seen.append(attempt)

    with pytest.raises(AgentExecutionFailed):
        await execute_with_retry(
            agent, make_envelope(), on_failure=on_failure, backoff_ms=0,
        )

    assert attempts_seen == [1, 2]
    assert state is State.SUSPENDED


@pytest.mark.asyncio
async def test_cancelled_error_is_not_retried():
    class CancellingAgent:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, envelope: AgentEnvelope) -> str:
            self.calls += 1
            raise asyncio.CancelledError

    agent = CancellingAgent()
    with pytest.raises(asyncio.CancelledError):
        await execute_with_retry(agent, make_envelope(), backoff_ms=0)
    assert agent.calls == 1, "取消不是失败：重试会吞掉取消信号"


def test_retry_budget_matches_the_state_machine_structure():
    """表允许的失败次数（= 总尝试次数）必须等于 RETRY_BUDGET + 1。

    RETRY_BUDGET 是状态机结构的事实而不是配置项：改预算必须同时改表，
    否则这条守卫会红。
    """
    failures_tolerated = 0
    state = State.EXECUTING
    while state is not State.SUSPENDED:
        state = next_state(state, Event.TASK_FAILED)
        failures_tolerated += 1

    assert failures_tolerated == RETRY_BUDGET + 1
    # 重试成功的那条边也必须存在，否则"重试一次"只有失败分支
    assert next_state(State.RETRY, Event.TASK_SUCCESS) is State.OUTPUT_READY
