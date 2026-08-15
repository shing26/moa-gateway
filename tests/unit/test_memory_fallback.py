import pytest

from app.redis_state.memory_fallback import MemoryStateStore


class TestMemoryStateStore:
    @pytest.mark.asyncio
    async def test_get_set_and_delete(self):
        store = MemoryStateStore()
        await store.set("key1", "value1")
        assert await store.get("key1") == "value1"
        await store.delete("key1")
        assert await store.get("key1") is None

    @pytest.mark.asyncio
    async def test_list_operations(self):
        store = MemoryStateStore()
        await store.lpush("list1", "a")
        await store.lpush("list1", "b")
        assert await store.llen("list1") == 2
        assert await store.lpop("list1") == "b"
        assert await store.llen("list1") == 1

    @pytest.mark.asyncio
    async def test_lrange(self):
        store = MemoryStateStore()
        await store.lpush("lst", "c")
        await store.lpush("lst", "b")
        await store.lpush("lst", "a")
        items = await store.lrange("lst", 0, -1)
        assert items == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_hash_operations(self):
        store = MemoryStateStore()
        await store.hset("hash1", {"field1": "val1", "field2": "val2"})
        assert await store.hget("hash1", "field1") == "val1"
        assert await store.hget("hash1", "field2") == "val2"

    @pytest.mark.asyncio
    async def test_exists(self):
        store = MemoryStateStore()
        await store.set("k", "v")
        assert await store.exists("k") is True
        await store.delete("k")
        assert await store.exists("k") is False

    @pytest.mark.asyncio
    async def test_ping(self):
        store = MemoryStateStore()
        assert await store.ping() is True

    @pytest.mark.asyncio
    async def test_eval_unsupported_script_returns_false(self):
        store = MemoryStateStore()
        assert await store.eval("unknown script", 1, "key", "val", "60") is False

    @pytest.mark.asyncio
    async def test_eval_acquire_release_and_extend_via_lock(self):
        from app.redis_state.lock import IdempotencyLock

        store = MemoryStateStore()
        lock = IdempotencyLock(redis=store, key="moa:lock:test", value="v1", ttl=30)

        assert await lock.acquire() is True
        assert lock.held is True
        assert await store.get("moa:lock:test") == "v1"
        assert await lock.extend(ttl=120) is True
        assert await lock.release() is True
        assert await store.get("moa:lock:test") is None

    @pytest.mark.asyncio
    async def test_eval_blocks_different_lock_value(self):
        from app.redis_state.lock import IdempotencyLock

        store = MemoryStateStore()
        lock_a = IdempotencyLock(redis=store, key="moa:lock:test", value="v1", ttl=30)
        lock_b = IdempotencyLock(redis=store, key="moa:lock:test", value="v2", ttl=30)

        assert await lock_a.acquire() is True
        assert await lock_b.acquire() is False
        assert lock_b.held is False

    @pytest.mark.asyncio
    async def test_eval_release_wrong_value_returns_zero(self):
        from app.redis_state.lock import IdempotencyLock

        store = MemoryStateStore()
        lock = IdempotencyLock(redis=store, key="moa:lock:test", value="v1", ttl=30)
        await lock.acquire()
        await store.set("moa:lock:test", "v2")

        assert await lock.release() is False
        assert await store.get("moa:lock:test") == "v2"

    @pytest.mark.asyncio
    async def test_set_with_ex_zero_expires_immediately(self):
        store = MemoryStateStore()
        await store.set("k", "v", ex=0)
        assert await store.get("k") is None
        assert await store.exists("k") is False

    @pytest.mark.asyncio
    async def test_expire_removes_key_when_ttl_zero(self):
        store = MemoryStateStore()
        await store.set("k", "v")
        await store.expire("k", 0)
        assert await store.get("k") is None

    @pytest.mark.asyncio
    async def test_close_clears_data(self):
        store = MemoryStateStore()
        await store.set("k", "v")
        await store.close()
        assert await store.get("k") is None

    @pytest.mark.asyncio
    async def test_key_helper(self):
        assert MemoryStateStore.key("s1") == "moa:s1"
        assert MemoryStateStore.key("s1", "custom") == "custom:s1"

    @pytest.mark.asyncio
    async def test_connect_returns_self(self):
        store = MemoryStateStore()
        result = await store.connect()
        assert result is store
