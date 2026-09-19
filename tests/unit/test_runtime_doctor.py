from __future__ import annotations

from scripts.doctor import (
    CheckResult,
    _safe_url,
    check_auth,
    check_port,
    exit_code,
)


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
