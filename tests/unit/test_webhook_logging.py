from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.deps import pipeline
import app.pipeline as pipeline_module
from app.guard.rbac import GuardianAction, GuardVerdict
from app.main import app
from app.vectordb.retriever import RetrievalResult
import app.routes.webhook as webhook_route


class FakeAgent:
    async def execute(self, envelope):
        return "agent reply"


class RaisingAgent:
    async def execute(self, envelope):
        raise RuntimeError("boom")


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
        return SimpleNamespace(score=1.0, need_human_review=False)

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
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    agent = RaisingAgent()
    _patch_pipeline(monkeypatch, agent)
    monkeypatch.setattr(pipeline_module, "log_request", fake_log)

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


def test_webhook_debug_text_not_500(monkeypatch) -> None:
    real_handle = pipeline.engine.handle_event
    _patch_pipeline(monkeypatch, FakeAgent())
    monkeypatch.setattr(pipeline.engine, "handle_event", real_handle)

    async def fake_score(*args, **kwargs):
        return SimpleNamespace(score=1.0, need_human_review=False)

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

    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.post(
            "/webhook/test",
            json={"session_id": "s-debug", "chat_id": "c-debug", "text": "帮我 debug 这个报错"},
        )
        assert res.status_code != 500
