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
