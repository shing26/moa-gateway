from __future__ import annotations

import pytest

import app.routes.health as health_module


class FakeRedisClient:
    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_healthz_caches_result_within_ttl(monkeypatch) -> None:
    connect_calls = []

    async def fake_connect(self):
        connect_calls.append(1)
        return FakeRedisClient()

    monkeypatch.setattr("app.redis_state.store.RedisStateStore.connect", fake_connect)
    monkeypatch.setattr(health_module, "_healthz_cache", {"at": 0.0, "result": None})

    first = await health_module.healthz()
    second = await health_module.healthz()

    assert first == second
    assert len(connect_calls) == 1


@pytest.mark.asyncio
async def test_healthz_uses_configured_redis_url(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class FakeStore:
        is_fallback = False

        def __init__(self, config) -> None:
            captured["url"] = config.url

        async def connect(self):
            return FakeRedisClient()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "app.redis_state.store.RedisStateStore",
        FakeStore,
    )
    monkeypatch.setattr(health_module.settings, "redis_url", "redis://:pw@cache:6380/2")
    monkeypatch.setattr(health_module, "_healthz_cache", {"at": 0.0, "result": None})

    result = await health_module.healthz()

    assert captured["url"] == "redis://:pw@cache:6380/2"
    assert result["checks"]["redis"] == "connected"
