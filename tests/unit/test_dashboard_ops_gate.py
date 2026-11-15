"""ADR-038 回归：dashboard ops 写入口的 ``DASHBOARD_OPS_WRITE`` 闸门。

双态覆盖（契约 A1/A2）：

* 开关关（未设置 / 非真值）→ 403，四个 ``os.environ`` 键一个都不能被改；
* 开关开（1/true/yes/on，大小写不敏感）→ 行为与修复前完全一致：写入生效。

真值语义复用 ``insecure_mode_enabled``，与 ``GATEWAY_ALLOW_INSECURE`` 同一套
fail-closed 姿势——这里钉住它，防止以后有人单独放宽本开关的接受集合。
"""

from __future__ import annotations

import os

import pytest

from app.main import app
from tests.support import app_client

_FULL_BODY = {
    "provider": "openai_compatible",
    "model": "gate-model",
    "base_url": "http://localhost:9/v1",
    "api_key": "sk-gate",
}
# fixture 钉住的已知值；关态用例据此断言"一个键都没被改"。
_PINNED = {
    "LLM_PROVIDER": "direct",
    "LLM_MODEL": "keep-model",
    "LLM_BASE_URL": "http://localhost:9/v1",
    "LLM_API_KEY": "sk-keep",
}


@pytest.fixture
def pinned_llm_env(monkeypatch):
    """把四个 LLM 键钉在已知值上；用例内直接改 os.environ，teardown 统一还原。"""
    for key, value in _PINNED.items():
        monkeypatch.setenv(key, value)


def _post(client):
    return client.post("/dashboard/api/ops/config", json=_FULL_BODY)


@pytest.mark.parametrize("raw", [None, "", "0", "false", "no", "off", " maybe "])
def test_ops_write_gate_closed_rejects_and_leaves_env_untouched(monkeypatch, raw):
    for key, value in _PINNED.items():
        monkeypatch.setenv(key, value)
    if raw is None:
        monkeypatch.delenv("DASHBOARD_OPS_WRITE", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_OPS_WRITE", raw)
    with app_client(app) as client:
        res = _post(client)
    assert res.status_code == 403
    body = res.json()
    assert body["ok"] is False
    assert body["error"] == "ops_write_disabled"
    assert "DASHBOARD_OPS_WRITE" in body["reason"]
    # 契约 A1 的硬断言：拒绝路径上四个 os.environ 键一个都不能被改。
    for key, expected in _PINNED.items():
        assert os.environ.get(key) == expected, key


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "ON", " on "])
def test_ops_write_gate_open_writes_env_like_before(monkeypatch, raw):
    for key, value in _PINNED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DASHBOARD_OPS_WRITE", raw)
    with app_client(app) as client:
        res = _post(client)
    assert res.status_code == 200
    assert res.json()["ok"] is True
    # 开关开时行为与修复前一致：四个键按提交值落进 os.environ。
    assert os.environ["LLM_PROVIDER"] == "openai_compatible"
    assert os.environ["LLM_MODEL"] == "gate-model"
    assert os.environ["LLM_BASE_URL"] == "http://localhost:9/v1"
    assert os.environ["LLM_API_KEY"] == "sk-gate"
    # 响应快照反映写入后的运行时状态（既有契约，见 test_dashboard_routes）。
    snapshot = res.json()["llm"]
    assert snapshot["provider"] == "openai_compatible"
    assert snapshot["api_key_set"] is True


def test_ops_config_get_endpoint_is_not_gated(pinned_llm_env):
    """闸门只管写入；GET 展示端点保持原状（契约 Forbidden：不删 GET 端点）。"""
    with app_client(app) as client:
        res = client.get("/dashboard/api/ops/config")
    assert res.status_code == 200
    assert "llm" in res.json()
