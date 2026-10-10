"""``/api/v1/collab`` 三个端点的契约测试。

重点不在"能不能跑通"（那是 test_collaboration.py 的事），而在**门**：

* ``COLLAB_ENABLED=0``（默认）时必须 503，不能因为装了 langgraph 就默认开启；
* 鉴权必须是继承来的，不是本模块自己写的一套。所以未带凭据要 401——
   ``/api/`` 前缀本来就在 ``_DASH_PROTECTED_PREFIXES`` 里，这里断言的是
   "新端点没有不小心绕过它"。
"""

from __future__ import annotations

import pytest

from app.main import app
from app.routes import collab as collab_route
from tests.support import app_client, auth_headers

from fastapi.testclient import TestClient


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.ran: list[dict] = []
        self.resumed: list[tuple[str, str]] = []

    async def run(self, **kwargs):
        self.ran.append(kwargs)
        return _result(status="ok")

    async def resume(self, thread_id: str, decision: str):
        self.resumed.append((thread_id, decision))
        return _result(status="approved")

    def pending_payload(self, thread_id: str):
        return {"kind": "collab_hitl", "trace_id": thread_id} if thread_id == "t-pending" else None


def _result(status: str):
    from app.orchestration.collaboration import CollabResult

    return CollabResult(
        trace_id="t-1",
        task="做点事",
        plan=(),
        rounds=1,
        per_agent_outputs={"coder": "ok"},
        critique_history=(),
        final_text=f"delivered ({status})",
        status=status,
        need_human_review=False,
        cost_usd=0.0,
        llm_latency_ms=0.0,
        tool_calls=0,
        node_path=("plan", "deliver"),
    )


def test_run_returns_503_when_collab_disabled_by_default(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "collab_enabled", False, raising=False)
    with app_client(app) as client:
        res = client.post("/api/v1/collab", json={"task": "x"})
    assert res.status_code == 503
    assert "COLLAB_ENABLED" in res.json()["message"]


def test_run_rejects_empty_task(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "collab_enabled", True, raising=False)
    monkeypatch.setattr(collab_route, "_orchestrator", _FakeOrchestrator())
    with app_client(app) as client:
        res = client.post("/api/v1/collab", json={"task": "   "})
    assert res.status_code == 400


def test_run_returns_the_full_result_shape(monkeypatch) -> None:
    from app.config import settings

    fake = _FakeOrchestrator()
    monkeypatch.setattr(settings, "collab_enabled", True, raising=False)
    monkeypatch.setattr(collab_route, "_orchestrator", fake)
    with app_client(app) as client:
        res = client.post(
            "/api/v1/collab",
            json={"task": "写函数", "session_id": "web:demo", "channel": "web"},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["text"] == "delivered (ok)"
    assert body["node_path"] == ["plan", "deliver"]
    assert body["plan"] == []
    assert body["per_agent_outputs"] == {"coder": "ok"}
    assert fake.ran[0]["task"] == "写函数"
    assert fake.ran[0]["channel"] == "web"


def test_pending_lookup_404_for_unknown_trace(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "collab_enabled", True, raising=False)
    monkeypatch.setattr(collab_route, "_orchestrator", _FakeOrchestrator())
    with app_client(app) as client:
        res = client.get("/api/v1/collab/pending/nope")
    assert res.status_code == 404


def test_pending_lookup_returns_the_interrupt_payload(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "collab_enabled", True, raising=False)
    monkeypatch.setattr(collab_route, "_orchestrator", _FakeOrchestrator())
    with app_client(app) as client:
        res = client.get("/api/v1/collab/pending/t-pending")
    assert res.status_code == 200
    assert res.json()["pending"]["kind"] == "collab_hitl"


def test_approve_normalizes_the_decision(monkeypatch) -> None:
    from app.config import settings

    fake = _FakeOrchestrator()
    monkeypatch.setattr(settings, "collab_enabled", True, raising=False)
    monkeypatch.setattr(collab_route, "_orchestrator", fake)
    with app_client(app) as client:
        res = client.post("/api/v1/collab/approve", json={"trace_id": "t-1", "decision": "reject"})
    assert res.status_code == 200
    assert fake.resumed == [("t-1", "reject")]


def test_approve_requires_trace_id(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "collab_enabled", True, raising=False)
    monkeypatch.setattr(collab_route, "_orchestrator", _FakeOrchestrator())
    with app_client(app) as client:
        res = client.post("/api/v1/collab/approve", json={"trace_id": ""})
    assert res.status_code == 400


def test_collab_endpoints_are_behind_gateway_auth() -> None:
    """未带凭据必须 401：鉴权是中间件的白名单制，新端点不该绕过它。"""
    with TestClient(app) as client:
        res = client.post("/api/v1/collab", json={"task": "x"})
    assert res.status_code == 401


def test_dashboard_api_path_is_protected() -> None:
    """同样的门对已有的 /dashboard/api 也成立——证明这里继承的是同一套规则。"""
    with TestClient(app) as client:
        res = client.post("/dashboard/api/chat", json={"session_id": "s", "text": "hi"})
    assert res.status_code == 401


def test_auth_headers_are_accepted() -> None:
    headers = auth_headers()
    assert headers["X-Gateway-Token"]
    assert headers["Authorization"].startswith("Basic ")
