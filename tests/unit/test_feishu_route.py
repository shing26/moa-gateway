from __future__ import annotations

import json

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


def test_feishu_no_adapter_still_returns_ok(monkeypatch) -> None:
    _clear_seen()
    monkeypatch.setattr(feishu_route, "get_adapter", _fake_get_adapter(None))

    with TestClient(app) as client:
        res = client.post("/feishu/event", json=_event("hello"))
        assert res.status_code == 200
        assert res.json() == {"msg": "ok"}
