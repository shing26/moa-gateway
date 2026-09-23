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
    async def test_eval_acquire_release_and_extend(self):
        """内存回退必须模仿 Redis 的 SET NX EX / 条件 DEL / 条件 EXPIRE 语义。

        此前这组断言是**借 `IdempotencyLock` 驱动**的，而那个类在 2026-09-23 被删除
        （从未接入请求路径）；被测对象其实一直是 `MemoryStateStore.eval`，所以改成
        直接用脚本常量驱动 —— 覆盖面不变，也不再依赖一个已删的包装类。
        """
        from app.redis_state.memory_fallback import (
            _ACQUIRE_LUA,
            _EXTEND_LUA,
            _RELEASE_LUA,
        )

        store = MemoryStateStore()

        assert await store.eval(_ACQUIRE_LUA, 1, "moa:lock:test", "v1", "30") is True
        assert await store.get("moa:lock:test") == "v1"
        assert await store.eval(_EXTEND_LUA, 1, "moa:lock:test", "v1", "120") == 1
        assert await store.eval(_RELEASE_LUA, 1, "moa:lock:test", "v1") == 1
        assert await store.get("moa:lock:test") is None

    @pytest.mark.asyncio
    async def test_eval_acquire_is_exclusive(self):
        """已存在的 key 再 ACQUIRE 必须失败（NX 语义）——这正是"只允许一次"的基础。"""
        from app.redis_state.memory_fallback import _ACQUIRE_LUA

        store = MemoryStateStore()

        assert await store.eval(_ACQUIRE_LUA, 1, "k", "v1", "30") is True
        assert await store.eval(_ACQUIRE_LUA, 1, "k", "v2", "30") is False
        assert await store.get("k") == "v1", "失败的那次不能覆盖已持有的值"

    @pytest.mark.asyncio
    async def test_eval_release_wrong_value_returns_zero(self):
        """条件 DEL：值不匹配就不删（避免释放了别人的锁）。"""
        from app.redis_state.memory_fallback import _ACQUIRE_LUA, _RELEASE_LUA

        store = MemoryStateStore()
        await store.eval(_ACQUIRE_LUA, 1, "moa:lock:test", "v1", "30")
        await store.set("moa:lock:test", "v2")

        assert await store.eval(_RELEASE_LUA, 1, "moa:lock:test", "v1") == 0
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
