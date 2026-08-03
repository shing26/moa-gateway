import asyncio
import json

from fastapi.testclient import TestClient

from app.deps import knowledge_base
from app.main import app


class FakeAgent:
    def __init__(self) -> None:
        self.envelopes = []

    async def execute(self, envelope):
        self.envelopes.append(envelope)
        return "ok"


def _event(text: str) -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": "m-search-test",
                "chat_id": "c-search-test",
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            "sender": {"sender_id": {"user_id": "u-search-test"}},
        },
    }


async def _cleanup() -> None:
    for doc in await knowledge_base.list_docs():
        await knowledge_base.delete_doc(doc["id"])


def test_feishu_message_injects_knowledge_context(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    agent = FakeAgent()
    monkeypatch.setattr(feishu_route, "get_agent", lambda name: agent)
    asyncio.run(knowledge_base.add_document("search test doc", "moa gateway redis config guide"))
    try:
        with TestClient(app) as client:
            res = client.post("/feishu/event", json=_event("moa gateway redis config"))
            assert res.status_code == 200
        assert agent.envelopes
        envelope = agent.envelopes[0]
        assert "redis config guide" in envelope.global_summary
    finally:
        asyncio.run(_cleanup())


def test_feishu_message_writes_request_log(monkeypatch) -> None:
    import app.routes.feishu as feishu_route

    agent = FakeAgent()
    calls = []

    async def fake_log(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(feishu_route, "get_agent", lambda name: agent)
    monkeypatch.setattr(feishu_route, "log_request", fake_log)
    try:
        with TestClient(app) as client:
            res = client.post("/feishu/event", json=_event("moa gateway redis config"))
            assert res.status_code == 200
    finally:
        asyncio.run(_cleanup())
    assert calls
    call = calls[0]
    assert call[7] == "moa gateway redis config"
    assert call[8] == "ok"
    assert call[5]
    assert call[4]
