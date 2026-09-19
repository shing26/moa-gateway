from __future__ import annotations

import time

import pytest

from app.main import app
from app.routes.chat import web_session_id
from tests.support import app_client


def test_web_session_id_prefixes() -> None:
    assert web_session_id("abc") == "web:abc"
    assert web_session_id("web:abc") == "web:abc"
    assert web_session_id("  ") == ""
    assert web_session_id("") == ""


def test_chat_requires_text_and_session() -> None:
    with app_client(app) as client:
        res = client.post("/dashboard/api/chat", json={"session_id": "s", "text": "  "})
        assert res.status_code == 400
        assert res.json()["error"] == "validation_error"
        res = client.post("/dashboard/api/chat", json={"session_id": "", "text": "hi"})
        assert res.status_code == 400


def test_chat_pipeline_error_returns_structured_500(monkeypatch) -> None:
    """status=error 不再伪装成 200：结构化 500 携带 M1 错误码（N2 修复）。"""
    from app.pipeline import PipelineResult

    class _FailingPipeline:
        async def run(self, event, **kwargs):
            return PipelineResult(
                trace_id="trace-x", state="", intent="",
                text="model exploded", status="error",
                error_code="agent_failed",
            )

    monkeypatch.setattr("app.routes.chat.pipeline", _FailingPipeline())
    with app_client(app) as client:
        res = client.post("/dashboard/api/chat", json={"session_id": "s-err", "text": "hi"})
    assert res.status_code == 500
    data = res.json()
    assert data["error"] == "agent_failed"
    assert data["message"] == "model exploded"
    assert data["session_id"] == "web:s-err"
    assert data["trace_id"] == "trace-x"


def test_chat_routes_task_to_task_agent() -> None:
    sid = f"web:test-{int(time.time())}"
    with app_client(app) as client:
        res = client.post(
            "/dashboard/api/chat",
            json={"session_id": sid, "text": "帮我算 3*7 并且现在几点"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["session_id"] == sid
        assert data["intent"] == "task"
        assert data["status"] == "ok"
        assert "3*7 = 21" in data["text"]
        assert "任务完成报告" in data["text"]

        # 历史应包含本次对话（user + assistant 两条）
        hist = client.post(
            "/dashboard/api/chat/history", json={"session_id": sid}
        )
        assert hist.status_code == 200
        h = hist.json()["history"]
        assert any(m["role"] == "user" and "帮我算" in m["content"] for m in h)
        assert any(m["role"] == "assistant" and "任务完成报告" in m["content"] for m in h)


def test_chat_history_web_prefix_is_idempotent() -> None:
    with app_client(app) as client:
        res = client.post(
            "/dashboard/api/chat/history", json={"session_id": "web:no-such"}
        )
        assert res.status_code == 200
        assert res.json()["session_id"] == "web:no-such"
        assert res.json()["history"] == []
