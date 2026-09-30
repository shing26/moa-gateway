from __future__ import annotations

from scripts.doctor import (
    CheckResult,
    _safe_url,
    check_auth,
    check_llm,
    check_port,
    exit_code,
)


def test_check_llm_matches_tagged_model_against_bare_config_name(monkeypatch) -> None:
    """配置写裸名（moa-qwen）、`ollama list` 报带 tag 名（moa-qwen:latest）——必须对得上。

    此前只给**已装**名字补 `:latest`，不给**配置**名字补，于是假报
    "model 'moa-qwen' is not installed"，还提示一个根本不可行的 `ollama pull`
    （探索性验收 D9：模型明明装着，doctor 说没有）。
    """
    monkeypatch.setattr(
        "scripts.doctor._ollama_tags",
        lambda base_url: ["moa-qwen:latest", "qwen2.5:3b:latest"],
    )
    env = {
        "LLM_PROVIDER": "local",
        "LLM_MODEL": "moa-qwen",
        "LLM_BASE_URL": "http://localhost:11434/v1",
        "LLM_API_KEY": "x",
    }
    assert check_llm(env).status == "pass", check_llm(env)

    missing = check_llm({**env, "LLM_MODEL": "nope-model"})
    assert missing.status == "fail", "真没装的仍要报"


def test_safe_url_hides_credentials() -> None:
    safe = _safe_url("redis://:secret@localhost:6380/0")
    assert safe == "redis://localhost:6380/0"
    assert "secret" not in safe


def test_auth_requires_secrets_or_explicit_insecure_mode() -> None:
    failed = check_auth({})
    insecure = check_auth({"GATEWAY_ALLOW_INSECURE": "1"})
    configured = check_auth(
        {
            "WEBHOOK_AUTH_TOKEN": "token",
            "DASHBOARD_PASSWORD": "password",
        }
    )

    assert failed.status == "fail"
    assert insecure.status == "warn"
    assert configured.status == "pass"


def test_port_check_reports_invalid_and_busy_ports(monkeypatch) -> None:
    monkeypatch.setattr("scripts.doctor._tcp_reachable", lambda *args, **kwargs: False)
    available = check_port({"GATEWAY_PORT": "8081"})
    invalid = check_port({"GATEWAY_PORT": "not-a-port"})
    monkeypatch.setattr("scripts.doctor._tcp_reachable", lambda *args, **kwargs: True)
    monkeypatch.setattr("scripts.doctor._gateway_health", lambda *args, **kwargs: None)
    busy = check_port({"GATEWAY_PORT": "8080"})
    monkeypatch.setattr(
        "scripts.doctor._gateway_health",
        lambda *args, **kwargs: {"status": "ok", "version": "0.1.0"},
    )
    running = check_port({"GATEWAY_PORT": "8081"})

    assert available.status == "pass"
    assert invalid.status == "fail"
    assert busy.status == "fail"
    assert running.status == "pass"


def test_exit_code_strict_promotes_warnings() -> None:
    checks = [CheckResult("x", "warn", "warning")]
    assert exit_code(checks) == 0
    assert exit_code(checks, strict=True) == 1
    assert exit_code([CheckResult("x", "fail", "failure")]) == 1
