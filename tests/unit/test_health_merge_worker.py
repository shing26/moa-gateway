"""``/healthz`` 的合并 worker 检查与 compose 启动方式（T2/T3/T6）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import app.routes.health as health_module

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _FakeRedisClient:
    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class _FakeRedisStore:
    is_fallback = False

    def __init__(self, _config) -> None:
        return None

    async def connect(self):
        return _FakeRedisClient()

    async def close(self) -> None:
        return None


def _prepare_healthz(monkeypatch) -> None:
    monkeypatch.setattr("app.redis_state.store.RedisStateStore", _FakeRedisStore)
    monkeypatch.setattr(health_module, "_healthz_cache", {"at": 0.0, "result": None})


@pytest.mark.asyncio
async def test_merge_worker_running_when_enabled_and_heartbeat_fresh(monkeypatch) -> None:
    """T2：enabled + 心跳新鲜 → running，整体 healthy。"""
    from apps.code_review_pipeline import merge_preflight

    async def fresh_heartbeat(**_kwargs):
        return 1.0

    monkeypatch.setattr(health_module.settings, "code_review_merge_enabled", True)
    monkeypatch.setattr(health_module.settings, "code_review_merge_poll_interval_s", 60)
    monkeypatch.setattr(
        merge_preflight, "read_worker_heartbeat_age_s", fresh_heartbeat
    )
    _prepare_healthz(monkeypatch)

    result = await health_module.healthz()

    assert result["checks"]["merge_worker"] == "running"
    assert result["status"] == "healthy"


@pytest.mark.asyncio
async def test_merge_worker_degraded_when_enabled_but_heartbeat_missing(
    monkeypatch,
) -> None:
    """T2：enabled + 无心跳 → degraded 前缀，整体 degraded。"""
    from apps.code_review_pipeline import merge_preflight

    async def missing_heartbeat(**_kwargs):
        return None

    monkeypatch.setattr(health_module.settings, "code_review_merge_enabled", True)
    monkeypatch.setattr(health_module.settings, "code_review_merge_poll_interval_s", 60)
    monkeypatch.setattr(
        merge_preflight, "read_worker_heartbeat_age_s", missing_heartbeat
    )
    _prepare_healthz(monkeypatch)

    result = await health_module.healthz()

    assert result["checks"]["merge_worker"].startswith("degraded:")
    assert "app.merge_worker" in result["checks"]["merge_worker"]
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_merge_worker_disabled_is_healthy(monkeypatch) -> None:
    """T2：通道关闭是合法部署，disabled 必须算健康。"""
    monkeypatch.setattr(health_module.settings, "code_review_merge_enabled", False)
    _prepare_healthz(monkeypatch)

    result = await health_module.healthz()

    assert result["checks"]["merge_worker"] == "disabled"
    assert result["status"] == "healthy"


@pytest.mark.asyncio
async def test_merge_worker_check_failure_is_fail_soft(monkeypatch) -> None:
    """T3：读取抛异常时 healthz 不 5xx，只把该项降级成字符串。"""
    from apps.code_review_pipeline import merge_preflight

    async def exploding_heartbeat(**_kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(health_module.settings, "code_review_merge_enabled", True)
    monkeypatch.setattr(health_module.settings, "code_review_merge_poll_interval_s", 60)
    monkeypatch.setattr(
        merge_preflight, "read_worker_heartbeat_age_s", exploding_heartbeat
    )
    _prepare_healthz(monkeypatch)

    result = await health_module.healthz()

    assert result["checks"]["merge_worker"].startswith("degraded:")
    assert result["status"] == "degraded"


def _compose_service() -> dict:
    compose = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    return compose["services"]["merge-worker"]


def _service_env(service: dict) -> dict[str, str]:
    environment = service["environment"]
    if isinstance(environment, dict):
        return {str(key): str(value) for key, value in environment.items()}
    pairs: dict[str, str] = {}
    for entry in environment:
        key, _, value = str(entry).partition("=")
        pairs[key] = value
    return pairs


def test_compose_has_default_merge_worker_service() -> None:
    """T6：默认 `up -d` 就要包含该进程，所以不能挂在 profile 下。"""
    service = _compose_service()

    command = service["command"]
    if isinstance(command, list):
        command = " ".join(command)
    assert command == "python -m app.merge_worker"
    assert "profiles" not in service


def test_compose_merge_worker_waits_for_redis_and_postgres() -> None:
    """T6：依赖 redis/postgres 且要求健康，避免 worker 先于存储起来空转。"""
    depends_on = _compose_service()["depends_on"]

    assert {"redis", "postgres"} <= set(depends_on)
    assert depends_on["redis"]["condition"] == "service_healthy"
    assert depends_on["postgres"]["condition"] == "service_healthy"


def test_gateway_and_merge_worker_share_merge_channel_settings() -> None:
    """T8：网关的 /healthz 阈值必须与 worker 实际轮询间隔来自同一组变量。"""
    compose = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    gateway_env = _service_env(compose["services"]["gateway"])
    worker_env = _service_env(compose["services"]["merge-worker"])

    for key in (
        "CODE_REVIEW_MERGE_ENABLED",
        "CODE_REVIEW_MERGE_POLL_INTERVAL_S",
    ):
        assert key in gateway_env
        assert key in worker_env
        assert gateway_env[key] == worker_env[key]


def test_merge_worker_exits_instead_of_restart_looping() -> None:
    """T8：缺 GITHUB_TOKEN 时 worker fail-fast；不能配成无限重启。"""
    restart = _compose_service()["restart"]

    assert restart != "unless-stopped"
    assert restart == "no"


def test_env_template_documents_default_merge_worker_start() -> None:
    """T8：说明里不再让用户额外加 --profile merge。"""
    template = (PROJECT_ROOT / ".env.template").read_text(encoding="utf-8")

    assert "--profile merge" not in template
