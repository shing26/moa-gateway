from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.channels.base import ChannelMessage
from app.deps import pipeline
from app.main import app
from app.pipeline import PipelineResult
import app.routes.feishu as feishu_route


class FakeFeishuAdapter:
    def __init__(self):
        self.sent: list[ChannelMessage] = []

    async def send(self, message: ChannelMessage) -> bool:
        self.sent.append(message)
        return True


def _event(text: str, chat_id: str = "c-route-test") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "event_id": "m-route-test"},
        "event": {
            "message": {
                "message_id": "m-route-test",
                "chat_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            "sender": {"sender_id": {"user_id": "u-route-test"}},
        },
    }


def _fake_get_adapter(adapter):
    async def _get():
        return adapter

    return _get


def _clear_seen():
    feishu_route._seen_events.clear()


def test_encrypted_event_is_rejected_loudly() -> None:
    """加密模式未实现，收到加密体必须**明确报错**而不是静默丢弃。

    此前它落到 "ignored" 分支：配了加密后收不到任何事件、日志里也看不出原因（2026-09-23 修）。
    """
    with TestClient(app) as client:
        res = client.post("/feishu/event", json={"encrypt": "BASE64BLOB"})

    assert res.status_code == 400
    assert res.json()["error"] == "encrypted_events_unsupported"


@pytest.mark.asyncio
async def test_hitl_click_by_unauthorized_operator_is_refused_and_not_consumed(monkeypatch) -> None:
    """审批人白名单（非空即强制）：不在名单里的点击被拒，且**挂起记录不被消耗**。

    不消耗是关键——否则一个没有权限的人点一下就把待审批记录点没了，别人再也批不了。
    """
    from app.config import settings
    from app.engine import HitlRequest

    monkeypatch.setattr(settings, "hitl_approver_ids", ("ou_allowed",))
    store = feishu_route.engine.session_store
    store.store_hitl("authz-sess", HitlRequest(
        session_id="authz-sess", trace_id="authz-trace", agent_output="out",
        intent="coding", agent_name="coder", channel="feishu", target="chat",
    ))

    try:
        await feishu_route._process_hitl_card("approve", "authz-trace", "authz-sess", "ou_outsider")
        assert store.get_hitl("authz-trace") is not None, "无权限的点击不能消耗挂起记录"
    finally:
        store.remove_hitl("authz-trace")


@pytest.mark.asyncio
async def test_hitl_click_by_allowed_operator_proceeds(monkeypatch) -> None:
    from app.config import settings
    from app.engine import HitlRequest

    monkeypatch.setattr(settings, "hitl_approver_ids", ("ou_allowed",))
    store = feishu_route.engine.session_store
    store.store_hitl("authz-ok", HitlRequest(
        session_id="authz-ok", trace_id="authz-ok-trace", agent_output="out",
        intent="coding", agent_name="coder", channel="feishu", target="chat",
    ))
    # 会话推到 SUSPENDED，好让 decide_hitl 不是"已失效"分支
    from app.fsm.state_machine import Event as FsmEvent
    from app.models.events import MoAEvent

    await feishu_route.engine.handle_event(MoAEvent(
        trace_id="authz-ok-trace", event=FsmEvent.MESSAGE_RECEIVED,
        session_id="authz-ok", text="", context={},
    ))
    await feishu_route.engine.handle_event(MoAEvent(
        trace_id="authz-ok-trace", event=FsmEvent.NEEDS_HUMAN,
        session_id="authz-ok", text="", context={},
    ))

    try:
        await feishu_route._process_hitl_card("approve", "authz-ok-trace", "authz-ok", "ou_allowed")
        assert store.get_hitl("authz-ok-trace") is None, "有权限的点击应当消耗记录"
    finally:
        store.remove_hitl("authz-ok-trace")


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_command_sends_reply(monkeypatch) -> None:
    _clear_seen()
    adapter = FakeFeishuAdapter()
    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("/coding"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}

    assert len(adapter.sent) == 1
    msg = adapter.sent[0]
    assert msg.channel == "feishu"
    assert msg.target == "c-route-test"
    assert "编程" in msg.text
    assert msg.trace_id


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_pipeline_review_sends_pending_hint(monkeypatch) -> None:
    _clear_seen()
    adapter = FakeFeishuAdapter()

    async def fake_run(event, *, channel, target, request=None):
        return PipelineResult(
            trace_id=event.trace_id, state="SUSPENDED", intent="coding",
            text="Output requires human approval before delivery",
            status="pending_review", need_human_review=True,
        )

    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))
    monkeypatch.setattr(pipeline, "run", fake_run)

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("帮我执行代码"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "输出需要人工审批"


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_pipeline_error_sends_friendly_text(monkeypatch) -> None:
    _clear_seen()
    adapter = FakeFeishuAdapter()

    async def fake_run(event, *, channel, target, request=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))
    monkeypatch.setattr(pipeline, "run", fake_run)

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("hello"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}

    assert len(adapter.sent) == 1
    assert "出错了" in adapter.sent[0].text


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_pipeline_blocked_sends_reason(monkeypatch) -> None:
    _clear_seen()
    adapter = FakeFeishuAdapter()

    async def fake_run(event, *, channel, target, request=None):
        return PipelineResult(
            trace_id=event.trace_id, state="ROUTED", intent="coding",
            text="resource 'guard' requires admin role", status="blocked",
        )

    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(adapter))
    monkeypatch.setattr(pipeline, "run", fake_run)

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("hello"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}

    assert len(adapter.sent) == 1
    assert "admin role" in adapter.sent[0].text


# 2026-09-23 起不再 skip：conftest 已隔离 FEISHU_VERIFICATION_TOKEN 并显式开 insecure，
# 这条用例本来就不需要真实环境（自己 monkeypatch 了 adapter）。
def test_feishu_no_adapter_still_returns_ok(monkeypatch) -> None:
    _clear_seen()
    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(None))

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("hello"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}
