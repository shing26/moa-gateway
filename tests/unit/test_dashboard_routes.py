import asyncio
import json
import os

from fastapi.testclient import TestClient

from app.agents import provider
from app.deps import command_mode, knowledge_base, memory
from app.main import app


def test_dashboard_pages_render():
    with TestClient(app) as client:
        for path in ("/dashboard", "/dashboard/overview", "/dashboard/knowledge",
                     "/dashboard/sessions", "/dashboard/test", "/dashboard/logs", "/dashboard/ops"):
            res = client.get(path)
            assert res.status_code == 200
            assert "MoA Gateway" in res.text


def test_dashboard_static_assets_served():
    with TestClient(app) as client:
        assert client.get("/dashboard/static/tokens.css").status_code == 200
        assert client.get("/dashboard/static/dashboard.css").status_code == 200
        assert client.get("/dashboard/static/dashboard.js").status_code == 200


def test_dashboard_logs_api_returns_list():
    with TestClient(app) as client:
        res = client.get("/dashboard/api/logs")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data.get("logs"), list)


def test_dashboard_upload_and_delete_document():
    with TestClient(app) as client:
        res = client.post("/dashboard/upload", json={"title": "dashboard test", "content": "hello world"})
        assert res.status_code == 200
        doc_id = res.json()["id"]
        docs = client.get("/knowledge/list").json()["documents"]
        assert any(d["id"] == doc_id for d in docs)
        deleted = client.post("/dashboard/delete", json={"doc_id": doc_id})
        assert deleted.json()["ok"] is True


def test_dashboard_clear_session_clears_memory_and_mode():
    sid = "test-session-clear"
    memory.add(sid, "hi", "hello")
    command_mode.set(sid, "coder")
    assert memory.get_history(sid)
    assert command_mode.get(sid) == "coder"
    with TestClient(app) as client:
        res = client.post(f"/dashboard/api/sessions/{sid}/clear")
        assert res.status_code == 200
        assert res.json()["ok"] is True
    assert memory.get_history(sid) == []
    assert command_mode.get(sid) is None


def test_dashboard_session_detail_and_mode():
    sid = "test-session-detail"
    memory.add(sid, "user q", "assistant a")
    try:
        with TestClient(app) as client:
            detail = client.get(f"/dashboard/api/sessions/{sid}")
            assert detail.status_code == 200
            data = detail.json()
            assert data["id"] == sid
            assert any(h["role"] == "user" and h["content"] == "user q" for h in data["history"])
            page = client.get(f"/dashboard/sessions/{sid}")
            assert page.status_code == 200
            assert "会话详情" in page.text
            mode = client.post(f"/dashboard/api/sessions/{sid}/mode", json={"mode": "coder"})
            assert mode.status_code == 200
            assert mode.json()["mode"] == "coder"
            assert command_mode.get(sid) == "coder"
    finally:
        memory.clear(sid)
        command_mode.clear(sid)


def test_dashboard_knowledge_file_upload_detail_search():
    with TestClient(app) as client:
        res = client.post(
            "/dashboard/api/knowledge/upload_file",
            files={"file": ("demo.md", b"# demo\nredis config guide", "text/markdown")},
        )
        assert res.status_code == 200
        doc_id = res.json()["id"]
        try:
            detail = client.get(f"/dashboard/api/knowledge/{doc_id}")
            assert detail.status_code == 200
            assert detail.json()["doc"]["title"] == "demo.md"
            page = client.get(f"/dashboard/knowledge/{doc_id}")
            assert page.status_code == 200
            assert "文档详情" in page.text
            search = client.post("/dashboard/api/knowledge/search", json={"query": "redis"})
            assert search.status_code == 200
            assert search.json()["doc_count"] >= 1
        finally:
            asyncio.run(knowledge_base.delete_doc(doc_id))


def test_dashboard_ops_config_update():
    saved = (
        os.environ.get("LLM_MODEL"),
        os.environ.get("OPENAI_BASE_URL"),
        os.environ.get("OPENAI_API_KEY"),
    )
    try:
        with TestClient(app) as client:
            res = client.post(
                "/dashboard/api/ops/config",
                json={
                    "model": "test-model",
                    "base_url": "http://localhost:9/v1",
                    "api_key": "sk-test",
                },
            )
            assert res.status_code == 200
            body = res.json()["llm"]
            assert body["model"] == "test-model"
            assert body["base_url"] == "http://localhost:9/v1"
            assert body["api_key_set"] is True
            cfg = client.get("/dashboard/api/ops/config").json()
            assert cfg["llm"]["model"] == "test-model"
            assert "sk-test" not in json.dumps(cfg)
            page = client.get("/dashboard/ops")
            assert page.status_code == 200
            assert "Provider" in page.text
    finally:
        for key, value in zip(
            ("LLM_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY"),
            saved,
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_dashboard_flag_set_and_delete():
    with TestClient(app) as client:
        res = client.post("/dashboard/api/ops/flags/evaluator.enabled", json={"value": False})
        assert res.status_code == 200
        cfg = client.get("/dashboard/api/ops/config").json()
        flag = next(f for f in cfg["flags"] if f["name"] == "evaluator.enabled")
        assert flag["value"] is False
        deleted = client.delete("/dashboard/api/ops/flags/evaluator.enabled")
        assert deleted.status_code == 200
        cfg = client.get("/dashboard/api/ops/config").json()
        flag = next(f for f in cfg["flags"] if f["name"] == "evaluator.enabled")
        assert flag["value"] is True


def test_dashboard_ops_test_message(monkeypatch):
    async def fake_chat(self, messages, **kwargs):
        return "pong"

    monkeypatch.setattr(provider.LLMClient, "chat", fake_chat)
    with TestClient(app) as client:
        res = client.post("/dashboard/api/ops/test", json={"message": "hi"})
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert data["reply"] == "pong"
