from types import SimpleNamespace

import json
import time
import os

import pytest

from app.deps import pipeline
import app.pipeline as pipeline_module
from app.engine import HitlRequest
from app.evaluator.evaluator import EvalResult
from app.guard.rbac import GuardianAction, GuardVerdict
from app.main import app
from app.vectordb.retriever import RetrievalResult
import app.routes.webhook as webhook_route
from tests.support import app_client


class FakeAgent:
    async def execute(self, envelope):
        return "agent reply"


class RaisingAgent:
    async def execute(self, envelope):
        raise RuntimeError("boom")


class FakeCardSender:
    """替掉真实飞书卡片发送器：失败升级会走卡片通道，测试不该打真网络。"""

    def __init__(self):
        self.cards = []

    async def send_card(self, card):
        self.cards.append(card)


def _patch_pipeline(monkeypatch, agent):
    async def fake_rate(key):
        return (True, 10)

    async def fake_handle(event):
        return SimpleNamespace(context=SimpleNamespace(state=SimpleNamespace(value="ROUTED")))

    async def fake_route(text):
        return ("coding", False)

    async def fake_retrieve(*args, **kwargs):
        return RetrievalResult(chunks=[], context="", doc_count=0)

    async def fake_flag(*args, **kwargs):
        return False

    monkeypatch.setattr(webhook_route.rate_limiter, "check", fake_rate)
    monkeypatch.setattr(pipeline.engine, "handle_event", fake_handle)
    monkeypatch.setattr(pipeline.command_mode, "get", lambda sid: None)
    monkeypatch.setattr(pipeline.router, "route", fake_route)
    monkeypatch.setattr(pipeline_module, "get_agent", lambda name: agent)
    monkeypatch.setattr(pipeline.retriever, "retrieve", fake_retrieve)
    monkeypatch.setattr(pipeline.flag_client, "get", fake_flag)
    monkeypatch.setattr(pipeline, "card_sender", FakeCardSender(), raising=False)
    monkeypatch.setattr(
        pipeline_module,
        "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt=""), "stable"),
    )
    return SimpleNamespace(
        fake_rate=fake_rate,
        fake_handle=fake_handle,
        fake_route=fake_route,
        fake_retrieve=fake_retrieve,
        fake_flag=fake_flag,
    )


def test_webhook_writes_request_log_for_agent_flow(monkeypatch) -> None:
    calls = []

    async def fake_score(*args, **kwargs):
        return EvalResult(score=1.0, need_human_review=False)

    def fake_adapt(*args, **kwargs):
        return SimpleNamespace(text="hello reply")

    async def fake_log(*args, **kwargs):
        calls.append(args)

    agent = FakeAgent()
    _patch_pipeline(monkeypatch, agent)
    monkeypatch.setattr(pipeline.evaluator, "score", fake_score)
    monkeypatch.setattr(
        pipeline.guard_service,
        "evaluate",
        lambda *a, **k: GuardVerdict(action=GuardianAction.ALLOW, reason="ok", role=None),
    )
    monkeypatch.setattr(pipeline.adapter, "adapt", fake_adapt)
    monkeypatch.setattr(pipeline_module, "log_request", fake_log)

    with app_client(app) as client:
        res = client.post(
            "/webhook/feishu",
            json={"session_id": "s1", "chat_id": "c1", "text": "hello"},
        )
        assert res.status_code == 200

    assert calls
    call = calls[0]
    assert call[7] == "hello"
    assert call[8] == "hello reply"
    assert call[5] == "coding"
    assert call[4]
    assert call[6] == "allow"


def test_webhook_escalates_agent_failure_to_hitl(monkeypatch) -> None:
    """重试预算耗尽 → 升级人工（ADR-010），不再只回一句 500。"""
    from app.config import settings

    # 升级路径依赖审批开关，而它默认 false——测试必须自己定死，别依赖本机 .env
    monkeypatch.setattr(settings, "hitl_enabled", True)
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append((args, kwargs))

    agent = RaisingAgent()
    _patch_pipeline(monkeypatch, agent)
    monkeypatch.setattr(pipeline_module, "log_request", fake_log)

    with app_client(app, raise_server_exceptions=False) as client:
        res = client.post(
            "/webhook/feishu",
            json={"session_id": "s1", "chat_id": "c1", "text": "hello"},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "pending_review"

    assert calls
    args, kwargs = calls[0]
    assert args[1] == 200
    assert args[7] == "hello"
    assert args[6] == "review"
    assert kwargs["retry_count"] == 1, "首次尝试 + 一次重试 = 1 次重试"
    assert kwargs["hitl_kind"] == "failure_escalation"
    assert "boom" in kwargs["retry_reason"]


class _JsonRequest:
    """够用的 Request 替身：处理器只用到 await request.json() 与 method/url。"""

    method = "POST"
    url = "http://test/webhook/callback"

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


@pytest.mark.asyncio
async def test_webhook_callback_expired_hitl_is_invalidated_not_500(monkeypatch) -> None:
    """重启后点旧卡片：不能 500，要作废记录并明确告知已失效。

    回归（2026-09-23）：此前这里是裸 `await engine.handle_event(...)`，而重启后
    FSM 会话状态（进程内）已丢，`INIT + HUMAN_APPROVED` 非法迁移 → 500；更糟的是
    挂起记录留在 Redis 里，卡片点一次错一次。
    """
    import app.routes.webhook as webhook_route

    calls = []

    async def fake_log(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(webhook_route, "log_request", fake_log)

    req = HitlRequest(
        session_id="expired-sess", trace_id="expired-trace", agent_output="out",
        intent="coding", agent_name="coder", channel="feishu", target="chat_1",
    )
    store = webhook_route.engine.session_store
    store.store_hitl("expired-sess", req)
    # 模拟"服务重启后会话状态丢失"
    webhook_route.engine.reset_session("expired-sess")

    body = {
        "action": {
            "value": {
                "session_id": "expired-sess",
                "trace_id": "expired-trace",
                "action": "approve",
            }
        }
    }
    try:
        resp = await webhook_route.webhook_callback(_JsonRequest(body))
    finally:
        store.remove_hitl("expired-trace")

    assert resp.status_code == 200, "失效不是服务器错误，不该 500"
    assert json.loads(resp.body)["status"] == "expired"
    assert calls and calls[0][1]["guard_action"] == "hitl_expired"
    assert store.get_hitl("expired-trace") is None, "失效的挂起记录必须被消耗掉"


async def _suspended_engine_setup(store, session_id: str, trace_id: str) -> HitlRequest:
    """把会话推到 SUSPENDED 并放一条挂起记录，返回该记录（否则 decide 会判 expired）。"""
    import app.routes.webhook as webhook_route
    from app.fsm.state_machine import Event as FsmEvent
    from app.models.events import MoAEvent

    engine = webhook_route.engine
    req = HitlRequest(
        session_id=session_id, trace_id=trace_id, agent_output="output text",
        intent="coding", agent_name="coder", channel="feishu", target="chat_1",
    )
    store.store_hitl(session_id, req)
    await engine.handle_event(
        MoAEvent(
            trace_id=trace_id, event=FsmEvent.MESSAGE_RECEIVED,
            session_id=session_id, text="", context={},
        )
    )
    await engine.handle_event(
        MoAEvent(
            trace_id=trace_id, event=FsmEvent.NEEDS_HUMAN,
            session_id=session_id, text="", context={},
        )
    )
    return req


def _callback_body(session_id: str, trace_id: str, action: str) -> dict:
    return {
        "action": {
            "value": {"session_id": session_id, "trace_id": trace_id, "action": action}
        }
    }


@pytest.mark.asyncio
async def test_webhook_callback_reject_path_returns_rejected(monkeypatch) -> None:
    """拒签路径必须能走通。

    回归（2026-09-23 自查发现）：G1 那次把 `session_state` 改名 `session_context`
    时漏改了这一分支 → NameError → 500，而当时唯一覆盖回调的用例是 skip 的，
    所以"全绿"掩盖了它。
    """
    import app.routes.webhook as webhook_route

    calls = []

    async def fake_log(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(webhook_route, "log_request", fake_log)

    store = webhook_route.engine.session_store
    session_id, trace_id = "rej-sess", "rej-trace"
    await _suspended_engine_setup(store, session_id, trace_id)

    try:
        resp = await webhook_route.webhook_callback(
            _JsonRequest(_callback_body(session_id, trace_id, "reject"))
        )
    finally:
        store.remove_hitl(trace_id)

    assert resp.status_code == 200
    payload = json.loads(resp.body)
    assert payload["status"] == "rejected"
    assert payload["state"] == "REJECTED"
    assert calls and calls[0][1]["guard_action"] == "hitl_reject"


@pytest.mark.asyncio
async def test_webhook_callback_second_click_is_rejected_without_double_delivery(monkeypatch) -> None:
    """连点两次：第一次认领成功，第二次 404 且**不重复写决策审计**。

    此前是"读 → 判断 → 删"三段，两个并发回调都能通过 → 重复送达 + 双审计。
    """
    import app.routes.webhook as webhook_route

    calls = []

    async def fake_log(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(webhook_route, "log_request", fake_log)

    store = webhook_route.engine.session_store
    session_id, trace_id = "twice-sess", "twice-trace"
    await _suspended_engine_setup(store, session_id, trace_id)
    body = _callback_body(session_id, trace_id, "approve")

    try:
        first = await webhook_route.webhook_callback(_JsonRequest(body))
        second = await webhook_route.webhook_callback(_JsonRequest(body))
    finally:
        store.remove_hitl(trace_id)

    assert first.status_code == 200
    assert second.status_code == 404, "同一个挂起请求只能被认领一次"
    assert len(calls) == 1, "第二次点击不该再写决策审计"


@pytest.mark.asyncio
async def test_webhook_callback_refuses_operator_outside_allowlist(monkeypatch) -> None:
    """审批人白名单（非空即强制）：名单外的点击 → 403，且**挂起记录不被消耗**。

    与 /feishu/event 同一语义；v1 卡片动作把点击者放在顶层 `open_id`。
    """
    import app.routes.webhook as webhook_route
    from app.config import settings

    monkeypatch.setattr(settings, "hitl_approver_ids", ("ou_allowed",))

    store = webhook_route.engine.session_store
    session_id, trace_id = "authz-sess", "authz-trace"
    await _suspended_engine_setup(store, session_id, trace_id)

    body = _callback_body(session_id, trace_id, "approve")
    body["open_id"] = "ou_outsider"
    try:
        resp = await webhook_route.webhook_callback(_JsonRequest(body))
        assert resp.status_code == 403
        assert store.get_hitl(trace_id) is not None, "无权限的点击不能消耗挂起记录"
    finally:
        store.remove_hitl(trace_id)


def test_webhook_debug_text_not_500(monkeypatch) -> None:
    real_handle = pipeline.engine.handle_event
    _patch_pipeline(monkeypatch, FakeAgent())
    monkeypatch.setattr(pipeline.engine, "handle_event", real_handle)

    async def fake_score(*args, **kwargs):
        return EvalResult(score=1.0, need_human_review=False)

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline.evaluator, "score", fake_score)
    monkeypatch.setattr(
        pipeline.guard_service,
        "evaluate",
        lambda *a, **k: GuardVerdict(action=GuardianAction.ALLOW, reason="ok", role=None),
    )
    monkeypatch.setattr(
        pipeline.adapter, "adapt", lambda *a, **k: SimpleNamespace(text="hello reply")
    )
    monkeypatch.setattr(pipeline_module, "log_request", fake_log)

    with app_client(app, raise_server_exceptions=False) as client:
        res = client.post(
            "/webhook/test",
            json={"session_id": "s-debug", "chat_id": "c-debug", "text": "帮我 debug 这个报错"},
        )
        assert res.status_code != 500


# 2026-09-23 起不再 skip：它自己 monkeypatch 了网络与 FSM，本就不需要真实环境。
# 值得记一笔：这条用例走 approve 路径，**本可以**抓到我上一批把 session_state 改名时
# 漏改拒签分支造成的 NameError——只因为它当时是 skip 而没抓到。
def test_webhook_callback_approve_logs_hitl_decision_and_duration(monkeypatch) -> None:
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append((args, kwargs))

    async def fake_handle(event):
        return SimpleNamespace(context=SimpleNamespace(state=SimpleNamespace(value="EXECUTING")))

    req = HitlRequest(
        session_id="log-sess", trace_id="log-trace", agent_output="callback output",
        intent="run_code", agent_name="coder", channel="feishu", target="chat_1",
        created_at=time.time() - 3.0,
    )
    store = pipeline.engine.session_store
    store.store_hitl("log-sess", req)
    monkeypatch.setattr(pipeline.engine, "handle_event", fake_handle)
    monkeypatch.setattr(webhook_route, "log_request", fake_log)
    try:
        with app_client(app) as client:
            res = client.post(
                "/webhook/callback",
                json={"action": {"value": {"session_id": "log-sess", "trace_id": "log-trace", "action": "approve"}}},
            )
        assert res.status_code == 200
        assert res.json()["status"] == "approved"
        assert res.json()["text"] == "callback output"
    finally:
        store.remove_hitl("log-trace")

    assert calls
    args, kwargs = calls[0]
    assert kwargs["hitl_decision"] == "approve"
    assert kwargs["hitl_duration_ms"] > 0
    assert kwargs["agent_name"] == "coder"
    assert kwargs["intent"] == "run_code"
    assert kwargs["guard_action"] == "hitl_approve"
