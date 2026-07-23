import pytest

from app.redis_state.stack import get_stack_depth, pop_state, push_state, reset_stack


class FakeRedis:
    def __init__(self):
        self.store: dict[str, Any] = {}
        self.lists: dict[str, list[str]] = {}

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
