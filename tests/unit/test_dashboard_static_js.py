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


def test_detail_delete_toast_survives_redirect() -> None:
    js = _dashboard_js()
    assert "sessionStorage.setItem('moaToast', '已删除文档')" in js
    assert "sessionStorage.getItem('moaToast')" in js


def test_obsidian_sync_disabled_shows_clear_message() -> None:
    js = _dashboard_js()
    assert "data.enabled === false" in js
    assert "Obsidian 未启用，未执行同步" in js
