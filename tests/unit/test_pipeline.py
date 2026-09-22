from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.pipeline as pipeline_module
from app.engine import Engine
from app.evaluator.evaluator import EvalResult
from app.fsm.state_machine import Event as FsmEvent
from app.guard.guard_service import GuardService
from app.guard.rbac import GuardianAction, GuardVerdict
from app.models.events import MoAEvent, new_trace_id
from app.outbound.adapter import ResponseAdapter
from app.pipeline import MoAPipeline
from app.vectordb.retriever import RetrievalResult


class FakeRetriever:
    async def retrieve(self, query, session_id=None, user_id=None):
        return RetrievalResult(chunks=[], context="retrieved context", doc_count=0)


class FakeFlagClient:
    async def get(self, name, default=False):
        return False


class FakeEvaluator:
    def __init__(self, need_human_review=False, score=1.0, issues=()):
        self.need_human_review = need_human_review
        self._result = EvalResult(
            score=score, need_human_review=need_human_review, issues=tuple(issues),
        )

    async def score(self, output_text, intent):
        # 返回真实 EvalResult 而不是 SimpleNamespace 的部分形状：Evaluator 协议声明
        # score() -> EvalResult，桩少一个字段就会在"链路真的用它"时才发现（2026-09-22
        # 接 eval_score 到审计时，四个文件的桩因为缺 issues 当场红）。
        return self._result


class FakeMemory:
    def __init__(self):
        self.added = []
        self.cleared = []

    def get_history(self, session_id):
        return []

    def add(self, session_id, user_msg, assistant_msg):
        self.added.append((session_id, user_msg, assistant_msg))

    def clear(self, session_id):
        self.cleared.append(session_id)


class FakeRouter:
    def __init__(self, intent="coding", fallback="regex"):
        self.intent = intent
        self.fallback = fallback

    async def route(self, text):
        return (self.intent, self.fallback)


class FakeCommandMode:
    def __init__(self):
        self.store = {}

    def set(self, session_id, mode):
        self.store[session_id] = mode

    def get(self, session_id):
        return self.store.get(session_id)

    def clear(self, session_id):
        self.store.pop(session_id, None)


class FakeGuard:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def evaluate(self, agent_name, intent, payload, *, hitl_enabled=True):
        self.calls.append((agent_name, intent, payload, hitl_enabled))
        return self.verdict


class OkAgent:
    async def execute(self, envelope):
        return "agent reply"


class RecordingAgent:
    def __init__(self):
        self.envelopes = []

    async def execute(self, envelope):
        self.envelopes.append(envelope)
        return "agent reply"


class RaisingAgent:
    async def execute(self, envelope):
        raise RuntimeError("boom")


class FakeCardSender:
    def __init__(self):
        self.cards = []

    async def send_card(self, card):
        self.cards.append(card)


def make_pipeline(
    engine=None,
    router=None,
    memory=None,
    guard=None,
    command_mode=None,
    card_sender=None,
    agent=None,
    evaluator=None,
):
    return MoAPipeline(
        engine=engine or Engine(),
        router=router or FakeRouter(),
        memory=memory or FakeMemory(),
        adapter=ResponseAdapter(),
        evaluator=evaluator or FakeEvaluator(),
        retriever=FakeRetriever(),
        prompt_registry=object(),
        flag_client=FakeFlagClient(),
        guard_service=guard or FakeGuard(GuardVerdict(action=GuardianAction.ALLOW, reason="ok")),
        command_mode=command_mode or FakeCommandMode(),
        card_sender=card_sender,
    )


def make_event(text="hello", session_id="s1"):
    return MoAEvent(
        trace_id=new_trace_id(), event=FsmEvent.MESSAGE_RECEIVED,
        session_id=session_id, text=text,
        context={"source": "test"},
    )


def patch_agents(monkeypatch, agent):
    monkeypatch.setattr(
        pipeline_module, "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt="sys"), "stable"),
    )
    monkeypatch.setattr(pipeline_module, "get_agent", lambda name: agent)


@pytest.mark.asyncio
async def test_ok_path(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    memory = FakeMemory()
    p = make_pipeline(memory=memory)
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "ok"
    assert result.text == "agent reply"
    # ADR-010：执行期状态真的走完了 ROUTED→EXECUTING→OUTPUT_READY→COMPLETED
    assert result.state == "COMPLETED"
    assert result.intent == "coding"
    assert result.need_human_review is False
    assert result.fallback == "regex"
    assert result.retry_count == 0
    assert memory.added == [("s1", "hello", "agent reply")]
    assert agent.envelopes[0].agent_local_slot["intent"] == "coding"


def _pipeline_with(engine, evaluator, guard):
    return MoAPipeline(
        engine=engine,
        router=FakeRouter(),
        memory=FakeMemory(),
        adapter=ResponseAdapter(),
        evaluator=evaluator,
        retriever=FakeRetriever(),
        prompt_registry=object(),
        flag_client=FakeFlagClient(),
        guard_service=guard,
        command_mode=FakeCommandMode(),
    )


@pytest.mark.asyncio
async def test_evaluator_review_brakes_into_hitl(monkeypatch):
    """评估器判定必须真的刹车：此前 need_human_review 只是响应体里的一个字段，
    输出照样以 status="ok" 送达用户（评估器"想升级人工"的意图没有接线到刹车）。"""
    patch_agents(monkeypatch, OkAgent())
    engine = Engine()
    p = _pipeline_with(
        engine,
        FakeEvaluator(need_human_review=True, issues=("empty_output",)),
        FakeGuard(GuardVerdict(action=GuardianAction.ALLOW, reason="ok")),
    )
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "pending_review"
    assert result.need_human_review is True
    assert result.guard_action == "review"
    assert result.hitl_kind == "eval_review"
    stored = engine.session_store.get_hitl(result.trace_id)
    assert stored is not None and stored.hitl_kind == "eval_review"


@pytest.mark.asyncio
async def test_evaluator_dangerous_output_is_denied_not_reviewed(monkeypatch):
    """AST 危险类 issue 不可审批：可执行危险代码是"绝不能交付"，不是"要不要批准"。"""
    patch_agents(monkeypatch, OkAgent())
    p = _pipeline_with(
        Engine(),
        FakeEvaluator(
            need_human_review=True, score=0.0, issues=("dangerous_call:exec",),
        ),
        FakeGuard(GuardVerdict(action=GuardianAction.ALLOW, reason="ok")),
    )
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "blocked"
    assert result.guard_action == "deny"
    assert result.hitl_kind == "eval_deny"


@pytest.mark.asyncio
async def test_guard_deny_outranks_evaluator_review(monkeypatch):
    """优先级 DENY > REVIEW：策略拦截胜过评估器的质量提醒。"""
    patch_agents(monkeypatch, OkAgent())
    p = _pipeline_with(
        Engine(),
        FakeEvaluator(need_human_review=True, issues=("empty_output",)),
        FakeGuard(GuardVerdict(action=GuardianAction.DENY, reason="blocked by policy")),
    )
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "blocked"
    # 请求侧 DENY 时根本不做输出/评估判定，所以来源不标记为评估器
    assert result.hitl_kind == ""


def test_verdict_from_eval_issues_classification():
    """分级：AST 危险类 → DENY（不可审批）；其余 issue → REVIEW；干净 → None。"""
    from app.pipeline import _verdict_from_eval_issues

    assert _verdict_from_eval_issues(()) is None
    assert _verdict_from_eval_issues(None) is None

    review = _verdict_from_eval_issues(("empty_output", "output_too_long"))
    assert review is not None and review.action == GuardianAction.REVIEW

    for issue in (
        "dangerous_call:exec",
        "dangerous_method:system",
        "dangerous_import:os",
        "dangerous_import_from:subprocess",
        "write_mode_open:w",
    ):
        verdict = _verdict_from_eval_issues((issue,))
        assert verdict is not None and verdict.action == GuardianAction.DENY, issue


@pytest.mark.asyncio
async def test_command_switch_mode(monkeypatch):
    cmd = FakeCommandMode()
    p = make_pipeline(command_mode=cmd)
    result = await p.run(make_event(text="/coding"), channel="test", target="s1")
    assert result.status == "command"
    assert result.intent == "coder"
    assert result.state == "ROUTED"
    assert result.text == "已切换至 编程 模式"
    assert cmd.store["s1"] == "coding"


@pytest.mark.asyncio
async def test_command_help(monkeypatch):
    p = make_pipeline()
    result = await p.run(make_event(text="/help"), channel="test", target="s1")
    assert result.status == "command"
    assert result.intent == "help"
    assert "可用指令" in result.text


@pytest.mark.asyncio
async def test_command_unknown(monkeypatch):
    p = make_pipeline()
    result = await p.run(make_event(text="/nope"), channel="test", target="s1")
    assert result.status == "command"
    assert result.intent == "help"
    assert "未知指令" in result.text


@pytest.mark.asyncio
async def test_command_mode_forces_intent(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    cmd = FakeCommandMode()
    cmd.store["s1"] = "coding"
    p = make_pipeline(router=FakeRouter(intent="assistant"), command_mode=cmd)
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.intent == "coding"
    assert agent.envelopes[0].agent_local_slot["intent"] == "coding"


@pytest.mark.asyncio
async def test_review_path_via_execution_marker(monkeypatch):
    class ExecuteCodeAgent:
        async def execute(self, envelope):
            return "EXECUTION_REQUIRES_APPROVAL: code=print('hi') lines=1"

    patch_agents(monkeypatch, ExecuteCodeAgent())
    engine = Engine()
    card_sender = FakeCardSender()
    guard = FakeGuard(GuardVerdict(action=GuardianAction.REVIEW, reason="needs approval"))
    p = make_pipeline(engine=engine, guard=guard, card_sender=card_sender)
    result = await p.run(make_event(), channel="test", target="t1")
    assert result.status == "pending_review"
    assert result.state == "SUSPENDED"
    assert result.text == "Output requires human approval before delivery"
    assert result.need_human_review is True
    stored = engine.session_store.get_hitl(result.trace_id)
    assert stored is not None
    assert "EXECUTION_REQUIRES_APPROVAL" in stored.agent_output
    assert stored.intent == "coding"
    assert stored.agent_name == "coder"
    assert stored.channel == "test"
    assert stored.target == "t1"
    assert len(card_sender.cards) == 1
    assert card_sender.cards[0].session_id == "s1"
    guard_intent = guard.calls[0][1]
    guard_hitl = guard.calls[0][3]
    assert guard_intent == "execute_code"
    assert guard_hitl is True


@pytest.mark.asyncio
async def test_review_without_marker_uses_intent_and_hitl_setting(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    guard = FakeGuard(GuardVerdict(action=GuardianAction.REVIEW, reason="needs approval"))
    p = make_pipeline(guard=guard)
    await p.run(make_event(), channel="test", target="t1")
    agent_name, guard_intent, payload, guard_hitl = guard.calls[0]
    assert agent_name == "coder"
    assert guard_intent == "coding"
    assert payload["role"] == "operator"
    assert payload["resource"] == "coding"


@pytest.mark.asyncio
async def test_review_via_real_guard_hitl_intent(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hitl_enabled", True)
    patch_agents(monkeypatch, OkAgent())
    engine = Engine()
    p = make_pipeline(
        engine=engine,
        router=FakeRouter(intent="write_file"),
        guard=GuardService(),
    )
    result = await p.run(make_event(), channel="test", target="t1")
    assert result.status == "pending_review"
    assert engine.session_store.get_hitl(result.trace_id) is not None


@pytest.mark.asyncio
async def test_review_same_session_twice_stores_both_by_trace_id(monkeypatch):
    class ExecuteCodeAgent:
        async def execute(self, envelope):
            return "EXECUTION_REQUIRES_APPROVAL: code=print('1')"

    patch_agents(monkeypatch, ExecuteCodeAgent())
    engine = Engine()
    guard = FakeGuard(GuardVerdict(action=GuardianAction.REVIEW, reason="needs approval"))
    p = make_pipeline(engine=engine, guard=guard)
    first = await p.run(make_event(session_id="s1"), channel="test", target="t1")
    second = await p.run(make_event(session_id="s1"), channel="test", target="t1")
    assert first.trace_id != second.trace_id
    first_stored = engine.session_store.get_hitl(first.trace_id)
    second_stored = engine.session_store.get_hitl(second.trace_id)
    assert first_stored is not None
    assert second_stored is not None
    assert first_stored.trace_id == first.trace_id
    assert second_stored.trace_id == second.trace_id


@pytest.mark.asyncio
async def test_deny_path(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    guard = FakeGuard(GuardVerdict(action=GuardianAction.DENY, reason="blocked by policy"))
    p = make_pipeline(guard=guard)
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "blocked"
    assert result.text == "blocked by policy"
    # 被拦截的输出已经有产出但未交付：状态停在 OUTPUT_READY（不借用 REJECTED，
    # 那个状态专指"人工拒绝"）。
    assert result.state == "OUTPUT_READY"


@pytest.mark.asyncio
async def test_deny_via_real_guard_sensitive_resource(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    p = make_pipeline(router=FakeRouter(intent="guard"), guard=GuardService())
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "blocked"
    assert "admin" in result.text


@pytest.mark.asyncio
async def test_agent_error_escalates_to_hitl_after_retry(monkeypatch):
    """重试预算耗尽 → 升级人工（ADR-010），不再静默回一句 error。"""
    from app.config import settings

    # HITL 开关默认 false（.env.template 的默认），升级路径依赖它，测试必须自己定死：
    # 本机 .env 设了 HITL_ENABLED=true 而 CI 没有，漏了这句就是非 hermetic 的用例。
    monkeypatch.setattr(settings, "hitl_enabled", True)
    patch_agents(monkeypatch, RaisingAgent())
    engine = Engine()
    card_sender = FakeCardSender()
    p = make_pipeline(engine=engine, card_sender=card_sender)
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "pending_review"
    assert result.text == "自动处理失败，已转人工处理"
    assert result.intent == "coding"
    assert result.state == "SUSPENDED"
    assert result.retry_count == 1, "首次尝试 + 一次重试 = 1 次重试"
    assert "boom" in result.retry_reason
    assert result.hitl_kind == "failure_escalation"
    stored = engine.session_store.get_hitl(result.trace_id)
    assert stored is not None
    assert stored.hitl_kind == "failure_escalation"
    assert "boom" in stored.agent_output
    assert len(card_sender.cards) == 1
    assert card_sender.cards[0].hitl_kind == "failure_escalation"


@pytest.mark.asyncio
async def test_agent_error_with_hitl_disabled_returns_error(monkeypatch):
    """HITL 关闭时没有人可以升级，保持原来的错误返回语义。"""
    from app.config import settings

    monkeypatch.setattr(settings, "hitl_enabled", False)
    patch_agents(monkeypatch, RaisingAgent())
    p = make_pipeline()
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "error"
    assert result.text == "agent execution failed"
    assert result.retry_count == 1


@pytest.mark.asyncio
async def test_reset_event_resets_session(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    memory = FakeMemory()
    cmd = FakeCommandMode()
    cmd.store["s1"] = "coder"
    p = make_pipeline(memory=memory, command_mode=cmd)
    event = MoAEvent(
        trace_id=new_trace_id(), event=FsmEvent.RESET,
        session_id="s1", text="cancel", context={"source": "test"},
    )
    result = await p.run(event, channel="test", target="s1")
    assert result.status == "reset"
    assert result.state == "INIT"
    assert result.text == "会话已重置"
    assert agent.envelopes == []
    assert memory.cleared == ["s1"]
    assert cmd.store == {}


@pytest.mark.asyncio
async def test_sensitive_event_suspends_without_executing(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    p = make_pipeline()
    event = MoAEvent(
        trace_id=new_trace_id(), event=FsmEvent.SENSITIVE_DETECTED,
        session_id="s2", text="debug 错误", context={"source": "test"},
    )
    result = await p.run(event, channel="test", target="s2")
    assert result.status == "suspended"
    assert result.state == "SUSPENDED"
    assert result.text == "检测到敏感内容，消息已挂起"
    assert agent.envelopes == []


@pytest.mark.asyncio
async def test_suspended_session_blocks_further_messages(monkeypatch):
    agent = RecordingAgent()
    patch_agents(monkeypatch, agent)
    engine = Engine()
    p = make_pipeline(engine=engine)
    await engine.handle_event(MoAEvent(
        trace_id=new_trace_id(), event=FsmEvent.SENSITIVE_DETECTED,
        session_id="s3", text="debug", context={},
    ))
    result = await p.run(make_event(session_id="s3"), channel="test", target="s3")
    assert result.status == "suspended"
    assert result.state == "SUSPENDED"
    assert agent.envelopes == []


@pytest.mark.asyncio
async def test_log_request_skipped_without_request(monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    patch_agents(monkeypatch, OkAgent())
    p = make_pipeline()
    await p.run(make_event(), channel="test", target="s1")
    assert calls == []


@pytest.mark.asyncio
async def test_log_request_called_with_request(monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    patch_agents(monkeypatch, OkAgent())
    p = make_pipeline()
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(), channel="test", target="s1", request=req)
    assert result.status == "ok"
    assert len(calls) == 1
    call = calls[0]
    assert call[0] is req
    assert call[1] == 200
    assert call[4] == "coder"
    assert call[5] == "coding"
    assert call[6] == "allow"
    assert call[7] == "hello"
    assert call[8] == "agent reply"


@pytest.mark.asyncio
async def test_log_request_receives_llm_metrics(monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)

    class MetricsAgent:
        async def execute(self, envelope):
            envelope.agent_local_slot["llm_metrics"] = {
                "model_used": "gpt-4o-mini",
                "cost_usd": 0.0123,
                "llm_latency_ms": 456.7,
                "fallback_used": "gpt-3.5-turbo",
            }
            return "agent reply"

    patch_agents(monkeypatch, MetricsAgent())
    p = make_pipeline()
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(), channel="test", target="s1", request=req)

    assert result.status == "ok"
    assert result.llm_model == "gpt-4o-mini"
    assert result.cost_usd == 0.0123
    assert result.llm_latency_ms == 456.7
    assert result.fallback_used == "gpt-3.5-turbo"
    assert len(calls) == 1
    assert calls[0]["llm_model"] == "gpt-4o-mini"
    assert calls[0]["cost_usd"] == 0.0123
    assert calls[0]["llm_latency_ms"] == 456.7
    assert calls[0]["fallback_used"] == "gpt-3.5-turbo"


@pytest.mark.asyncio
async def test_log_request_receives_eval_score_and_issues(monkeypatch):
    """评测器的返回值必须进审计（A4/A6）。

    此前 log_request 把 eval_score 写死 0.0、eval_issues 从不写入，于是真实流量里
    「评测跑过且判 1.0」与「压根没评测」在数据上分不开——两个字段都成了结构性哑字段
    （2026-09-21 的 12 维评估据此误判过一轮）。这条钉住「pipeline 真的把 evaluator
    的返回值透传下去了」。
    """
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    patch_agents(monkeypatch, OkAgent())
    evaluator = FakeEvaluator(score=0.3, issues=("contains_unfinished_marker",))
    p = make_pipeline(evaluator=evaluator)
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(), channel="test", target="s1", request=req)

    # 有 issue → 评估器判定为 REVIEW，输出进人工而不是直接送达（ADR-010 第二部分）
    assert result.status == "pending_review"
    assert len(calls) == 1
    assert calls[0]["eval_score"] == 0.3
    assert calls[0]["eval_issues"] == ("contains_unfinished_marker",)
    assert calls[0]["hitl_kind"] == "eval_review"


@pytest.mark.asyncio
async def test_log_request_records_tool_activity(monkeypatch):
    """工具活动必须进审计。

    tool_calls=3 / tool_errors=3 就是"三个工具全失败却仍返回了结果"——没有这两个
    数，"任务真的完成"与"优雅失败"在审计里无法区分。
    """
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append((args, kwargs))

    class ToolReportingAgent:
        async def execute(self, envelope):
            envelope.agent_local_slot["tool_calls_total"] = 3
            envelope.agent_local_slot["tool_errors_total"] = 3
            return "用了工具但还是没查到"

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    patch_agents(monkeypatch, ToolReportingAgent())
    p = make_pipeline()
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(), channel="test", target="s1", request=req)

    args, kwargs = calls[0]
    assert kwargs["tool_calls"] == 3
    assert kwargs["tool_errors"] == 3
    assert result.tool_calls == 3
    assert result.tool_errors == 3


@pytest.mark.asyncio
async def test_log_request_on_agent_failure_records_escalation(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hitl_enabled", True)
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    patch_agents(monkeypatch, RaisingAgent())
    p = make_pipeline()
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(), channel="test", target="s1", request=req)
    assert result.status == "pending_review"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == 200
    assert args[6] == "review"
    assert kwargs["retry_count"] == 1
    assert kwargs["hitl_kind"] == "failure_escalation"
    assert "boom" in kwargs["retry_reason"]


@pytest.mark.asyncio
async def test_command_logs_with_request(monkeypatch):
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(pipeline_module, "log_request", fake_log)
    p = make_pipeline()
    req = SimpleNamespace(method="POST", url="http://test/x")
    result = await p.run(make_event(text="/help"), channel="test", target="s1", request=req)
    assert result.status == "command"
    assert len(calls) == 1
    assert calls[0][1] == 200
    assert calls[0][4] == "command"
    assert calls[0][5] == "help"


@pytest.mark.asyncio
async def test_set_card_sender(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    sender = FakeCardSender()
    p = make_pipeline()
    p.set_card_sender(sender)
    assert p.card_sender is sender


class PolicyOutputAgent:
    def __init__(self, output):
        self.output = output

    async def execute(self, envelope):
        return self.output


@pytest.mark.asyncio
async def test_output_internal_ip_blocks_with_policy_hit(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hitl_enabled", False)
    patch_agents(monkeypatch, PolicyOutputAgent("服务器地址是 10.0.0.1"))
    p = make_pipeline(guard=GuardService())
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "blocked"
    assert "policy.security.internal_ip" in result.policy_hits


@pytest.mark.asyncio
async def test_output_price_commitment_reviews_with_policy_hit(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hitl_enabled", True)
    patch_agents(monkeypatch, PolicyOutputAgent("优惠价只要 99 元"))
    engine = Engine()
    p = make_pipeline(engine=engine, guard=GuardService())
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "pending_review"
    assert "policy.compliance.no_price_commitment" in result.policy_hits


@pytest.mark.asyncio
async def test_normal_output_has_empty_policy_hits(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    p = make_pipeline(guard=GuardService())
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "ok"
    assert result.policy_hits == ()


@pytest.mark.asyncio
async def test_output_policy_exception_keeps_pipeline_ok(monkeypatch):
    patch_agents(monkeypatch, OkAgent())
    guard = GuardService()

    def boom(text, *, intent="assistant", role=None, hitl_enabled=True):
        raise RuntimeError("policy engine exploded")

    monkeypatch.setattr(guard, "evaluate_output", boom)
    p = make_pipeline(guard=guard)
    result = await p.run(make_event(), channel="test", target="s1")
    assert result.status == "ok"
    assert result.policy_hits == ()
