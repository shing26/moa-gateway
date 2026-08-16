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


def test_ops_status_dots_use_real_state() -> None:
    js = _dashboard_js()
    assert "status-' + item.tone" in js
    assert "checks.redis" in js


def test_overview_polling_backs_off_after_failure() -> None:
    js = _dashboard_js()
    assert "Math.min(overviewInterval * 2, 30000)" in js
    assert "window.setTimeout(overviewTick" in js


def test_test_bench_has_timeout_feedback() -> None:
    js = _dashboard_js()
    assert "controller.abort()" in js
    assert "请求超时（超过 25 秒）" in js


def test_ops_test_shows_progress_after_wait() -> None:
    js = _dashboard_js()
    assert "仍在等待响应（正在尝试模型）" in js
