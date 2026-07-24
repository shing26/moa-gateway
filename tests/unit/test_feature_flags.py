from __future__ import annotations

import os
import time

import pytest

from app.feature_flags import FeatureFlagClient


@pytest.mark.asyncio
async def test_get_default_value():
    client = FeatureFlagClient()
    value = await client.get("nonexistent.flag", default=False)
    assert value is False


@pytest.mark.asyncio
async def test_get_set_and_retrieve():
    client = FeatureFlagClient()
    await client.set("test.flag", True)
    value = await client.get("test.flag", default=False)
    assert value is True


@pytest.mark.asyncio
async def test_invalidate_cache():
    client = FeatureFlagClient(_cache_ttl=60.0)
    await client.set("test.cache", "old")
    v1 = await client.get("test.cache")
    assert v1 == "old"
    await client.set("test.cache", "new")
    client.invalidate("test.cache")
    v2 = await client.get("test.cache")
    assert v2 == "new"


@pytest.mark.asyncio
async def test_invalidate_all():
    client = FeatureFlagClient()
    await client.set("flag.a", 1)
    await client.set("flag.b", 2)
    _ = await client.get("flag.a")
    _ = await client.get("flag.b")
    client.invalidate()
    assert len(client._cache) == 0


@pytest.mark.asyncio
async def test_seed_values():
    client = FeatureFlagClient()
    client.seed({"guard.enabled": True, "evaluator.enabled": False})
    assert await client.get("guard.enabled") is True
    assert await client.get("evaluator.enabled") is False


@pytest.mark.asyncio
async def test_parse_value_bool():
    client = FeatureFlagClient()
    await client.set("test.bool_true", "true")
    await client.set("test.bool_false", "false")
    await client.set("test.number", "42")
    await client.set("test.string", "hello")
    assert await client.get("test.bool_true") is True
    assert await client.get("test.bool_false") is False
    assert await client.get("test.number") == 42
    assert await client.get("test.string") == "hello"


@pytest.mark.asyncio
async def test_delete_removes_key():
    client = FeatureFlagClient()
    await client.set("test.temp", "value")
    assert await client.get("test.temp") == "value"
    await client.delete("test.temp")
    assert await client.get("test.temp", default=None) is None


@pytest.mark.asyncio
async def test_cache_ttl_expires():
    client = FeatureFlagClient(_cache_ttl=-0.1)
    await client.set("test.expire", "original")
    _ = await client.get("test.expire")
    await client.set("test.expire", "updated")
    value = await client.get("test.expire")
    assert value == "updated"
