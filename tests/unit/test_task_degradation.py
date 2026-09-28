"""task-LLM 降级可见性测试（ADR-018 决策 7）。

背景：``LiteLLMTaskLLM`` 的 ``decide`` / ``plan`` / ``summarize`` 都把异常吞掉，返回一个
外观正常的兜底结果——``decide`` 甚至返回 ``action="finish"``。于是"基础设施崩了"经
``TaskAgent`` → pipeline 一路以 ``status=ok`` 交付，和"模型答完了"在用户看来没有区别。

这里钉住三件事：① 降级必须留下原因；② 原因随返回值/回调走，**不挂在 LLM 实例上**
（``self._llm`` 是进程内单例，挂上去会被并发请求互相踩）；③ 一路传到 pipeline 的刹车。

注意：ADR-011 修的是**图路径**的降级可见性，task-LLM 这条路径当时漏了——"修一半 = 没修"。
"""

from __future__ import annotations

import pytest

from app.agent_core.litellm_llm import LiteLLMTaskLLM
from app.agent_core.mock_llm import MockTaskLLM
from app.agent_core.react import ReActLoop
from app.agent_core.task_agent import TaskAgent
from app.agent_core.types import ReActDecision
from app.agents.contract import AgentEnvelope
from app.agents.tools import ToolRegistry


class _BoomClient:
    """``chat`` 一律抛错：模拟 provider 挂掉。"""

    async def chat(self, messages):  # noqa: ANN001, ARG002
        raise RuntimeError("provider down")


def _envelope(text: str = "x") -> AgentEnvelope:
    return AgentEnvelope(
        trace_id="t-degraded",
        session_id="s-degraded",
        user_raw_input=text,
        global_summary="",
        agent_local_slot={},
    )


class _DegradingTaskLLM:
    """plan 与 decide 都降级，summarize 正常——覆盖两种上报通道。"""

    async def plan(self, *, task, on_degrade=None):  # noqa: ANN001
        if on_degrade is not None:
            on_degrade("plan 调用失败: provider down")
        return [task]

    async def decide(self, *, task, subtask, observations):  # noqa: ANN001, ARG002
        return ReActDecision(
            action="finish",
            final_answer="决策失败: provider down",
            degraded_reason="decide 调用失败: provider down",
        )

    async def summarize(self, *, task, plan, results, on_degrade=None):  # noqa: ANN001, ARG002
        return "汇总是完成了的样子"


# ── ① ReAct 循环把决策降级带出来 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_react_loop_records_degrade_reason() -> None:
    class DegradingLLM:
        async def decide(self, *, task, subtask, observations):  # noqa: ANN001, ARG002
            return ReActDecision(
                action="finish",
                final_answer="决策失败: boom",
                degraded_reason="decide 调用失败: boom",
            )

        async def plan(self, *, task, on_degrade=None):  # noqa: ANN001, ARG002
            return [task]

        async def summarize(self, *, task, plan, results, on_degrade=None):  # noqa: ANN001, ARG002
            return "x"

    loop = ReActLoop(DegradingLLM(), ToolRegistry(), max_steps=3, session_id="s1")
    result = await loop.run(task="x", subtask="x")

    assert result.degraded_reasons == ["decide 调用失败: boom"]
    assert result.tool_calls == 0, "降级不是工具尝试"


# ── ② LiteLLM 三处降级都要报 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decide_failure_is_marked_degraded() -> None:
    llm = LiteLLMTaskLLM(client=_BoomClient())
    decision = await llm.decide(task="x", subtask="x", observations=[])

    assert decision.action == "finish"
    assert decision.degraded_reason, "崩溃不得伪装成正常收尾"
    assert "provider down" in decision.degraded_reason


def test_malformed_json_is_marked_degraded() -> None:
    """模型没按协议输出 JSON，此前被当成"收尾答案"直接交付。"""
    llm = LiteLLMTaskLLM(client=_BoomClient())
    decision = llm._parse_decision("这不是 JSON")

    assert decision.final_answer == "这不是 JSON"
    assert decision.degraded_reason, "协议失败同样是降级，不是正常答案"


@pytest.mark.asyncio
async def test_plan_failure_reports_and_falls_back() -> None:
    llm = LiteLLMTaskLLM(client=_BoomClient())
    reasons: list[str] = []

    plan = await llm.plan(task="拆一下", on_degrade=reasons.append)

    assert plan == ["拆一下"], "降级后回退成单子任务"
    assert reasons and "provider down" in reasons[0]


@pytest.mark.asyncio
async def test_summarize_failure_reports() -> None:
    llm = LiteLLMTaskLLM(client=_BoomClient())
    reasons: list[str] = []

    text = await llm.summarize(
        task="x", plan=["a"], results=[], on_degrade=reasons.append
    )

    assert text, "兜底返回拼接文本，而不是抛错"
    assert reasons and "provider down" in reasons[0]


# ── ③ TaskAgent 把原因写进 slot（每请求一份，不共享）──────────────────────────


@pytest.mark.asyncio
async def test_task_agent_writes_degradation_reasons() -> None:
    agent = TaskAgent(llm=_DegradingTaskLLM(), max_steps=2)
    envelope = _envelope()

    await agent.execute(envelope)

    reasons = envelope.agent_local_slot.get("task_degradation_reasons")
    assert isinstance(reasons, list) and len(reasons) == 2
    assert any("plan" in r for r in reasons)
    assert any("decide" in r for r in reasons)


@pytest.mark.asyncio
async def test_task_agent_omits_key_when_healthy() -> None:
    """没降级时键必须**缺席**——缺席本身有含义（pipeline 据此判断要不要刹车）。"""
    agent = TaskAgent(llm=MockTaskLLM(), max_steps=2)
    envelope = _envelope("现在几点")

    await agent.execute(envelope)

    assert "task_degradation_reasons" not in envelope.agent_local_slot


@pytest.mark.asyncio
async def test_degradation_is_per_request_not_on_the_llm() -> None:
    """降级原因不得留在 LLM 实例上——它是进程内单例，会串到别的请求。"""
    llm = _DegradingTaskLLM()
    first, second = _envelope("第一次"), _envelope("第二次")

    await TaskAgent(llm=llm, max_steps=2).execute(first)
    await TaskAgent(llm=llm, max_steps=2).execute(second)

    assert first.agent_local_slot["task_degradation_reasons"] == second.agent_local_slot[
        "task_degradation_reasons"
    ]
    assert not hasattr(llm, "degradations"), "降级状态不该挂在共享的 LLM 实例上"
