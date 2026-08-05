from __future__ import annotations

from app.engine import RedisHitlStorage
from app.memory import _SHARED_BRIDGE, _SyncBridge, RedisConversationStorage


class FakeRedisClient:
    def __init__(self, fail_ping: bool = False) -> None:
        self.fail_ping = fail_ping
        self.data: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def ping(self) -> bool:
        if self.fail_ping:
            raise ConnectionError("no redis")
        return True

    async def aclose(self) -> None:
        pass

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    async def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)


class TestSharedSyncBridge:
    def test_shared_bridge_is_sync_bridge_instance(self):
        assert isinstance(_SHARED_BRIDGE, _SyncBridge)

    def test_shared_bridge_default_timeout_is_one_second(self):
        assert _SHARED_BRIDGE._timeout == 1.0

    def test_hitl_and_conversation_storage_use_the_same_bridge(self):
        hitl = RedisHitlStorage(client=FakeRedisClient())
        conv = RedisConversationStorage(client=FakeRedisClient())
        assert hitl._bridge is _SHARED_BRIDGE
        assert conv._bridge is _SHARED_BRIDGE
        assert hitl._bridge is conv._bridge

    def test_storage_default_timeouts_are_one_second(self):
        assert RedisHitlStorage()._timeout == 1.0
        assert RedisConversationStorage()._timeout == 1.0

    def test_shared_bridge_reuses_one_backing_thread(self):
        import threading

        bridge = _SHARED_BRIDGE
        bridge.call(FakeRedisClient().ping())
        thread = bridge._thread
        assert thread is not None and thread.is_alive()
        bridge.call(FakeRedisClient().ping())
        assert bridge._thread is thread
        running = [t for t in threading.enumerate()
                   if t.name == "moa-redis-bridge" and t.is_alive()]
        assert len(running) == 1


class TestRetryAfterFallback:
    def test_hitl_retries_after_memory_fallback_recovers(self):
        fake = FakeRedisClient(fail_ping=True)
        storage = RedisHitlStorage(client=fake, retry_after=0.0)
        assert storage.get("k") is None
        assert storage._using_memory is True
        fake.fail_ping = False
        storage.set("k", "v")
        assert storage._using_memory is False
        assert fake.data.get("k") == "v"

    def test_conversation_retries_after_memory_fallback_recovers(self):
        fake = FakeRedisClient(fail_ping=True)
        storage = RedisConversationStorage(client=fake, retry_after=0.0)
        storage.rpush("moa:mem:s1", "x")
        assert storage._using_memory is True
        assert "moa:mem:s1" in storage._memory
        fake.fail_ping = False
        storage.rpush("moa:mem:s1", "y")
        assert storage._using_memory is False
        assert fake.lists["moa:mem:s1"] == ["y"]

    def test_fallback_wait_window_prevents_immediate_retry(self):
        fake = FakeRedisClient(fail_ping=True)
        storage = RedisHitlStorage(client=fake, retry_after=300.0)
        assert storage.get("k") is None
        assert storage._using_memory is True
        fake.fail_ping = False
        assert storage.get("k") is None
        assert storage._using_memory is True