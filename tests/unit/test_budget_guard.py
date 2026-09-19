"""预算 guard（M6）：BudgetGuard 单元 + FSM/graph 集成 + 配置校验。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import app.pipeline as pipeline_module
from app.budget.guard import BudgetGuard
from app.engine import Engine
from app.fsm.state_machine import Event as FsmEvent
from app.guard.rbac import GuardianAction, GuardVerdict
from app.models.events import MoAEvent, new_trace_id
from app.orchestration.graph import LangGraphOrchestrator
from app.outbound.adapter import ResponseAdapter
from app.pipeline import MoAPipeline


# ── 单元 ────────────────────────────────────────────────────────────────────


def test_default_guard_only_accounts_never_blocks():
    guard = BudgetGuard(limit_usd=0.0)
    assert not guard.enforcing
    assert guard.record("s", 5.0) == 5.0
    # 只核算模式永远放行，即使累计已远超"限额"
    assert guard.check("s") is True


def test_over_limit_session_is_rejected_then_resets():
    guard = BudgetGuard(limit_usd=1.0)
    assert guard.check("s1") is True
    guard.record("s1", 0.6)
    assert guard.check("s1") is True
    guard.record("s1", 0.5)
    assert guard.spent("s1") == pytest.approx(1.1)
    assert guard.check("s1") is False
    guard.reset("s1")
    assert guard.check("s1") is True


def test_sessions_are_isolated_and_zero_cost_is_ignored():
    guard = BudgetGuard(limit_usd=1.0)
    guard.record("s1", 2.0)
    assert guard.check("s2") is True
    assert guard.record("s3", 0.0) == 0.0
    assert "s3" not in guard._spend


def test_ttl_eviction_expires_idle_sessions():
    guard = BudgetGuard(limit_usd=1.0, ttl_seconds=0.05)
    guard.record("s1", 0.5)
    time.sleep(0.06)
    assert guard.check("s1") is True  # 过期条目被清理后视为新会话
    assert guard.spent("s1") == 0.0


def test_negative_limit_is_config_error(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("BUDGET_SESSION_LIMIT_USD", raising=False)
    monkeypatch.setenv("BUDGET_SESSION_LIMIT_USD", "-1")
    with pytest.raises(ValueError, match="BUDGET_SESSION_LIMIT_USD"):
        Settings()
    monkeypatch.setenv("BUDGET_SESSION_LIMIT_USD", "0")
    assert Settings().budget_session_limit_usd == 0.0


# ── FSM 管道集成 ────────────────────────────────────────────────────────────


class FakeRetriever:
    async def retrieve(self, query, session_id=None, user_id=None):
        from app.vectordb.retriever import RetrievalResult

        return RetrievalResult(chunks=[], context="", doc_count=0)


class FakeFlagClient:
    async def get(self, name, default=False):
        return False


class FakeEvaluator:
    async def score(self, output_text, intent):
        return SimpleNamespace(score=1.0, need_human_review=False)


class FakeMemory:
    def get_history(self, session_id):
        return []

    def add(self, session_id, user_msg, assistant_msg):
        pass

    def clear(self, session_id):
        pass


class FakeRouter:
    async def route(self, text):
        return ("coding", "regex")


class FakeCommandMode:
    def set(self, session_id, mode):
        pass

    def get(self, session_id):
        return None

    def clear(self, session_id):
        pass


class FakeGuard:
    def evaluate(self, agent_name, intent, payload, *, hitl_enabled=True):
        return GuardVerdict(action=GuardianAction.ALLOW, reason="ok")


class CostAgent:
    """模拟一次带真实成本的 agent 调用（走 llm_metrics 通道）。"""

    def __init__(self, cost: float):
        self.cost = cost
        self.calls = 0

    async def execute(self, envelope):
        self.calls += 1
        envelope.agent_local_slot["llm_metrics"] = {
            "model_used": "fake-model",
            "cost_usd": self.cost,
            "llm_latency_ms": 10.0,
            "fallback_used": "",
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
        return "agent reply"


def make_pipeline(budget_guard, agent):
    return MoAPipeline(
        engine=Engine(),
        router=FakeRouter(),
        memory=FakeMemory(),
        adapter=ResponseAdapter(),
        evaluator=FakeEvaluator(),
        retriever=FakeRetriever(),
        prompt_registry=object(),
        flag_client=FakeFlagClient(),
        guard_service=FakeGuard(),
        command_mode=FakeCommandMode(),
        budget_guard=budget_guard,
    )


def make_event(text, session_id):
    return MoAEvent(
        trace_id=new_trace_id(),
        event=FsmEvent.MESSAGE_RECEIVED,
        session_id=session_id,
        text=text,
        context={"source": "test"},
    )


def patch_agents(monkeypatch, agent):
    monkeypatch.setattr(
        pipeline_module,
        "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt="sys"), "stable"),
    )
    monkeypatch.setattr(pipeline_module, "get_agent", lambda name: agent)


@pytest.mark.asyncio
async def test_pipeline_accumulates_cost_and_blocks_next_request(monkeypatch):
    agent = CostAgent(cost=0.6)
    patch_agents(monkeypatch, agent)
    guard = BudgetGuard(limit_usd=1.0)
    p = make_pipeline(guard, agent)

    first = await p.run(make_event("hello", "s1"), channel="test", target="s1")
    assert first.status == "ok"
    assert guard.spent("s1") == pytest.approx(0.6)

    second = await p.run(make_event("hello again", "s1"), channel="test", target="s1")
    assert second.status == "ok"  # 0.6 < 1.0 放行，执行后累计到 1.2
    assert guard.spent("s1") == pytest.approx(1.2)

    third = await p.run(make_event("third try", "s1"), channel="test", target="s1")
    assert third.status == "blocked"
    assert third.error_code == "budget_exceeded"
    assert third.text == "该会话的预算额度已用完，请求被拒绝"
    assert agent.calls == 2  # 第三次在 agent 执行前被拒

    fourth = await p.run(make_event("other session", "s2"), channel="test", target="s2")
    assert fourth.status == "ok"  # 会话之间互相隔离


@pytest.mark.asyncio
async def test_pipeline_without_guard_or_limit_zero_keeps_old_behavior(monkeypatch):
    agent = CostAgent(cost=0.6)
    patch_agents(monkeypatch, agent)
    for guard in (None, BudgetGuard(limit_usd=0.0)):
        p = make_pipeline(guard, agent)
        result = await p.run(make_event("hello", "s-free"), channel="test", target="s-free")
        assert result.status == "ok"
        assert result.error_code == ""


# ── LangGraph 路径 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_execute_node_short_circuits_on_budget():
    guard = BudgetGuard(limit_usd=0.01)
    guard.record("s1", 1.0)

    class _Self:
        _budget_guard = guard

    result = await LangGraphOrchestrator._node_execute(
        _Self(),
        {"session_id": "s1", "agent_name": "general", "text": "x"},
    )
    assert result["status"] == "blocked"
    assert result["error_code"] == "budget_exceeded"
    assert result["guard_action"] == "budget_exceeded"


def test_after_execute_routes_budget_block_to_blocked_node():
    assert LangGraphOrchestrator._after_execute({"error_code": "budget_exceeded"}) == "budget_blocked"
    assert LangGraphOrchestrator._after_execute({"error_code": ""}) == "ok"
    assert LangGraphOrchestrator._after_execute({}) == "ok"


@pytest.mark.asyncio
async def test_graph_records_cost_after_execute(monkeypatch):
    """正常执行路径要累计成本（node 内 record），这里直接验证 record 调用点。"""
    guard = BudgetGuard(limit_usd=1.0)

    class _Self:
        _budget_guard = guard
        _memory = SimpleNamespace(get_history=lambda sid: [])

    async def fake_execute(env):
        env.agent_local_slot["llm_metrics"] = {"cost_usd": 0.3}
        return "reply"

    monkeypatch.setattr(
        "app.orchestration.graph.get_agent", lambda name: SimpleNamespace(execute=fake_execute)
    )
    state = {"session_id": "s1", "trace_id": "t", "agent_name": "general", "text": "x", "intent": "coding"}
    result = await LangGraphOrchestrator._node_execute(_Self(), state)
    assert result["raw_output"] == "reply"
    assert guard.spent("s1") == pytest.approx(0.3)
