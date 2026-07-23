import pytest

import uuid
from typing import Any

from app.redis_state.lock import IdempotencyLock, LuaLockFactory
from app.redis_state.stack import get_stack_depth, pop_state, push_state, reset_stack


def _eval_acquire(store: dict[str, Any], key: str, value: str, ttl: str) -> bool:
    """Simulate Lua ACQUIRE: SET key value NX EX ttl."""
    if key in store:
        return False
    store[key] = value
    store[f"_ttl:{key}"] = int(ttl)
    return True


def _eval_release(store: dict[str, Any], key: str, value: str) -> int:
    """Simulate Lua RELEASE: DEL only if value matches."""
    if store.get(key) == value:
        del store[key]
        store.pop(f"_ttl:{key}", None)
        return 1
    return 0


def _eval_extend(store: dict[str, Any], key: str, value: str, ttl: str) -> int:
    """Simulate Lua EXTEND: EXPIRE only if value matches."""
    if store.get(key) == value:
        store[f"_ttl:{key}"] = int(ttl)
        return 1
    return 0


class FakeRedis:
    def __init__(self):
        self.store: dict[str, Any] = {}
        self.lists: dict[str, list[str]] = {}

    async def eval(self, script: str, numkeys: int, *args: str) -> Any:
        if "SET" in script and "NX" in script:
            return _eval_acquire(self.store, args[0], args[1], args[2])
        if "DEL" in script:
            return _eval_release(self.store, args[0], args[1])
        if "EXPIRE" in script:
            return _eval_extend(self.store, args[0], args[1], args[2])
        return False

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def lpop(self, key):
        items = self.lists.get(key)
        if not items:
            return None
        value = items.pop(0)
        if not items:
            del self.lists[key]
        return value

    async def lrange(self, key, start, end):
        return list(self.lists.get(key, [])[start : end + 1 if end != -1 else None])

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def expire(self, key, ttl):
        self.store.setdefault("ttl", {})[key] = ttl

    async def hset(self, key, mapping=None, **kwargs):
        target = mapping or kwargs
        self.store.setdefault("hash", {})[key] = target

    async def hget(self, key, field):
        return self.store.get("hash", {}).get(key, {}).get(field)

    async def delete(self, key):
        self.store.get("hash", {}).pop(key, None)
        self.lists.pop(key, None)


@pytest.mark.asyncio
async def test_push_pop_roundtrip():
    redis = FakeRedis()
    await push_state(redis, "s1", "SUSPENDED", {"x": 1})
    depth = await get_stack_depth(redis, "s1")
    assert depth == 1
    payload = await pop_state(redis, "s1")
    assert payload["state"] == "SUSPENDED"
    assert await get_stack_depth(redis, "s1") == 0


@pytest.mark.asyncio
async def test_reset_clears_stack():
    redis = FakeRedis()
    await push_state(redis, "s1", "SUSPENDED", {"x": 1})
    await push_state(redis, "s1", "EXECUTING", {"x": 2})
    await reset_stack(redis, "s1")
    assert await get_stack_depth(redis, "s1") == 0


@pytest.mark.asyncio
async def test_lock_acquire_and_release():
    redis = FakeRedis()
    lock = IdempotencyLock(redis=redis, key="moa:lock:test", value="v1", ttl=30)

    acquired = await lock.acquire()
    assert acquired is True
    assert lock.held is True

    released = await lock.release()
    assert released is True
    assert lock.held is False


@pytest.mark.asyncio
async def test_lock_idempotent_acquire_same_value():
    redis = FakeRedis()
    lock = IdempotencyLock(redis=redis, key="moa:lock:test", value="v1", ttl=30)

    first = await lock.acquire()
    assert first is True

    # Re-acquire with same value -> idempotent success
    second = await lock.acquire()
    assert second is True


@pytest.mark.asyncio
async def test_lock_blocks_different_value():
    redis = FakeRedis()
    lock_a = IdempotencyLock(redis=redis, key="moa:lock:test", value="v1", ttl=30)
    lock_b = IdempotencyLock(redis=redis, key="moa:lock:test", value="v2", ttl=30)

    await lock_a.acquire()
    blocked = await lock_b.acquire()
    assert blocked is False
    assert lock_b.held is False


@pytest.mark.asyncio
async def test_lock_extend():
    redis = FakeRedis()
    lock = IdempotencyLock(redis=redis, key="moa:lock:test", value="v1", ttl=30)

    await lock.acquire()
    extended = await lock.extend(ttl=120)
    assert extended is True


@pytest.mark.asyncio
async def test_lock_release_wrong_value_returns_zero():
    redis = FakeRedis()
    lock = IdempotencyLock(redis=redis, key="moa:lock:test", value="v1", ttl=30)

    await lock.acquire()
    # Manually overwrite the value in store to simulate another process
    redis.store["moa:lock:test"] = "v2"

    released = await lock.release()
    assert released is False


@pytest.mark.asyncio
async def test_lock_factory_creates_locks():
    redis = FakeRedis()
    factory = LuaLockFactory(redis, default_ttl=45)

    lock = factory.lock("my-session")
    assert lock.key == "moa:lock:my-session"
    assert lock.ttl == 45
    assert lock.value != ""  # auto-generated uuid

    acquired = await lock.acquire()
    assert acquired is True
    assert lock.held is True

    released = await lock.release()
    assert released is True
