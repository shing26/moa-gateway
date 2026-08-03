from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.guard.rbac import GuardianAction, GuardVerdict
from app.main import app
from app.vectordb.retriever import RetrievalResult


class FakeAgent:
    async def execute(self, envelope):
        return "agent reply"


class RaisingAgent:
    async def execute(self, envelope):
        raise RuntimeError("boom")


def test_webhook_writes_request_log_for_agent_flow(monkeypatch) -> None:
    import app.routes.webhook as webhook_route

    calls = []

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

    async def fake_score(*args, **kwargs):
        return SimpleNamespace(score=1.0, need_human_review=False)

    def fake_adapt(*args, **kwargs):
        return SimpleNamespace(text="hello reply")

    async def fake_log(*args, **kwargs):
        calls.append(args)

    agent = FakeAgent()
    monkeypatch.setattr(webhook_route.rate_limiter, "check", fake_rate)
    monkeypatch.setattr(webhook_route.engine, "handle_event", fake_handle)
    monkeypatch.setattr(webhook_route.command_mode, "get", lambda sid: None)
    monkeypatch.setattr(webhook_route.router, "route", fake_route)
    monkeypatch.setattr(webhook_route, "get_agent", lambda name: agent)
    monkeypatch.setattr(webhook_route._retriever, "retrieve", fake_retrieve)
    monkeypatch.setattr(webhook_route._flag_client, "get", fake_flag)
    monkeypatch.setattr(
        webhook_route,
        "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt=""), "stable"),
    )
    monkeypatch.setattr(webhook_route.evaluator, "score", fake_score)
    monkeypatch.setattr(
        webhook_route.guard_service,
        "evaluate",
        lambda *a, **k: GuardVerdict(action=GuardianAction.ALLOW, reason="ok", role=None),
    )
    monkeypatch.setattr(webhook_route.adapter, "adapt", fake_adapt)
    monkeypatch.setattr(webhook_route, "log_request", fake_log)

    with TestClient(app) as client:
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


def test_webhook_writes_request_log_on_agent_failure(monkeypatch) -> None:
    import app.routes.webhook as webhook_route

    calls = []

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

    async def fake_log(*args, **kwargs):
        calls.append(args)

    agent = RaisingAgent()
    monkeypatch.setattr(webhook_route.rate_limiter, "check", fake_rate)
    monkeypatch.setattr(webhook_route.engine, "handle_event", fake_handle)
    monkeypatch.setattr(webhook_route.command_mode, "get", lambda sid: None)
    monkeypatch.setattr(webhook_route.router, "route", fake_route)
    monkeypatch.setattr(webhook_route, "get_agent", lambda name: agent)
    monkeypatch.setattr(webhook_route._retriever, "retrieve", fake_retrieve)
    monkeypatch.setattr(webhook_route._flag_client, "get", fake_flag)
    monkeypatch.setattr(
        webhook_route,
        "select_canary_version",
        lambda *a, **k: (SimpleNamespace(system_prompt=""), "stable"),
    )
    monkeypatch.setattr(webhook_route, "log_request", fake_log)

    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.post(
            "/webhook/feishu",
            json={"session_id": "s1", "chat_id": "c1", "text": "hello"},
        )
        assert res.status_code == 500

    assert calls
    call = calls[0]
    assert call[1] == 500
    assert call[7] == "hello"
    assert call[6] == "error"
