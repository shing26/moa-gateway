from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _dashboard_js() -> str:
    with TestClient(app) as client:
        return client.get("/dashboard/static/dashboard.js").text


def test_fetchjson_shows_server_error_detail() -> None:
    js = _dashboard_js()
    assert "data.detail || data.error || data.message" in js
    assert "请求失败: " in js
