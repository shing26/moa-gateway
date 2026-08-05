from __future__ import annotations

import fnmatch
import json

import pytest

from app.engine import HitlRequest, RedisHitlStorage, SessionStore
from app.memory import _SHARED_BRIDGE, ConversationMemory, RedisConversationStorage


class FakeRedisClient:
    def __init__(self, fail_ping: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}
        self.fail_ping = fail_ping
        self.closed = False

    async def ping(self) -> bool:
        if self.fail_ping:
            raise ConnectionError("no redis")
        return True

    async def aclose(self) -> None:
        self.closed = True

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value
        if ex is not None:
            self.expires[key] = ex

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)
        self.lists.pop(key, None)
        self.expires.pop(key, None)

    async def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self._slice(self.lists.get(key, []), start, end)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.lists[key] = self._slice(self.lists.get(key, []), start, end)

    async def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl

    async def keys(self, pattern: str = "*") -> list[str]:
        all_keys = set(self.data) | set(self.lists) | set(self.expires)
        return sorted(k for k in all_keys if fnmatch.fnmatchcase(k, pattern))

    @staticmethod
    def _slice(items: list[str], start: int, end: int) -> list[str]:
        if not items:
            return []
        n = len(items)
        s = start if start >= 0 else max(0, n + start)
        e = n - 1 if end == -1 else (end if end >= 0 else n + end)
        return items[s : e + 1]


def _hitl(**overrides) -> HitlRequest:
    fields = {
        "session_id": "sess-1",
        "trace_id": "trace-1",
        "agent_output": "confidential data",
        "intent": "write_file",
        "agent_name": "coder",
        "channel": "feishu",
        "target": "chat_123",
    }
    fields.update(overrides)
    return HitlRequest(**fields)


class TestSessionStoreRedis:
    @pytest.mark.asyncio
    async def test_roundtrip_key_format_and_ttl(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        req = _hitl()
        store.store_hitl("sess-1", req)
        assert "moa:hitl:trace-1" in fake.data
        assert fake.expires.get("moa:hitl:trace-1") == RedisHitlStorage.DEFAULT_TTL
        got = store.get_hitl("trace-1")
        assert got == req
        store.remove_hitl("trace-1")
        assert store.get_hitl("trace-1") is None
        assert "moa:hitl:trace-1" not in fake.data

    @pytest.mark.asyncio
    async def test_custom_ttl(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake, ttl=120))
        store.store_hitl("sess-1", _hitl())
        assert fake.expires.get("moa:hitl:trace-1") == 120

    @pytest.mark.asyncio
    async def test_key_format(self):
        assert RedisHitlStorage.key("trace-1") == "moa:hitl:trace-1"
        assert RedisConversationStorage.key("sess-1") == "moa:mem:sess-1"

    @pytest.mark.asyncio
    async def test_json_payload_roundtrip_preserves_fields(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        req = _hitl(agent_output="secret", target="chat_999")
        store.store_hitl("sess-1", req)
        payload = json.loads(fake.data["moa:hitl:trace-1"])
        assert payload == {
            "session_id": "sess-1",
            "trace_id": "trace-1",
            "agent_output": "secret",
            "intent": "write_file",
            "agent_name": "coder",
            "channel": "feishu",
            "target": "chat_999",
        }
        assert store.get_hitl("trace-1") == req

    @pytest.mark.asyncio
    async def test_corrupt_payload_returns_none(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        fake.data["moa:hitl:trace-1"] = "not-json"
        assert store.get_hitl("trace-1") is None

    @pytest.mark.asyncio
    async def test_clear_all(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        store.store_hitl("s1", _hitl(session_id="s1", trace_id="t-1"))
        store.store_hitl("s2", _hitl(session_id="s2", trace_id="t-2"))
        store.clear_all()
        assert "moa:hitl:t-1" not in fake.data
        assert "moa:hitl:t-2" not in fake.data
        assert store.get_hitl("t-1") is None
        assert store.get_hitl("t-2") is None

    @pytest.mark.asyncio
    async def test_same_session_different_trace_ids_independent(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        store.store_hitl("sess-1", _hitl(session_id="sess-1", trace_id="trace-a", agent_output="first-out"))
        store.store_hitl("sess-1", _hitl(session_id="sess-1", trace_id="trace-b", agent_output="second-out"))
        assert "moa:hitl:trace-a" in fake.data
        assert "moa:hitl:trace-b" in fake.data
        assert store.get_hitl("trace-a").agent_output == "first-out"
        assert store.get_hitl("trace-b").agent_output == "second-out"
        store.remove_hitl("trace-b")
        assert store.get_hitl("trace-b") is None
        assert store.get_hitl("trace-a") is not None

    @pytest.mark.asyncio
    async def test_falls_back_to_memory_on_connect_failure(self):
        fake = FakeRedisClient(fail_ping=True)
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        req = _hitl()
        store.store_hitl("sess-1", req)
        assert "moa:hitl:trace-1" not in fake.data
        assert store.get_hitl("trace-1") == req
        store.remove_hitl("trace-1")
        assert store.get_hitl("trace-1") is None

    @pytest.mark.asyncio
    async def test_falls_back_when_set_fails_after_connect(self):
        class FlakyClient(FakeRedisClient):
            async def set(self, key, value, ex=None):
                raise ConnectionError("redis went away")

        fake = FlakyClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        store.store_hitl("sess-1", _hitl())
        assert store.get_hitl("trace-1") == _hitl()
        store.remove_hitl("trace-1")
        assert store.get_hitl("trace-1") is None

    @pytest.mark.asyncio
    async def test_fallback_disabled_raises(self):
        fake = FakeRedisClient(fail_ping=True)
        store = SessionStore(storage=RedisHitlStorage(client=fake, enable_fallback=False))
        with pytest.raises(ConnectionError):
            store.store_hitl("sess-1", _hitl())

    def test_storage_constructor_error_falls_back_to_memory(self):
        def broken_factory():
            raise RuntimeError("no redis available")

        store = SessionStore(storage=broken_factory)
        req = _hitl()
        store.store_hitl("sess-1", req)
        assert store.get_hitl("trace-1") == req
        store.remove_hitl("trace-1")
        assert store.get_hitl("trace-1") is None

    def test_default_storage_is_memory(self):
        store = SessionStore()
        req = _hitl()
        store.store_hitl("sess-1", req)
        assert store._pending_hitl["trace-1"] is req
        assert store.get_hitl("trace-1") is req
        store.clear_all()
        assert store.get_hitl("trace-1") is None


class TestConversationMemoryRedis:
    @pytest.mark.asyncio
    async def test_rpush_lrange_trim_and_ttl(self):
        fake = FakeRedisClient()
        mem = ConversationMemory(max_turns=2, storage=RedisConversationStorage(client=fake))
        mem.add("s1", "u1", "a1")
        mem.add("s1", "u2", "a2")
        mem.add("s1", "u3", "a3")
        key = "moa:mem:s1"
        assert key in fake.lists
        assert fake.expires.get(key) == RedisConversationStorage.DEFAULT_TTL
        assert len(fake.lists[key]) == 4
        assert json.loads(fake.lists[key][0]) == {"role": "user", "content": "u2"}
        assert json.loads(fake.lists[key][-1]) == {"role": "assistant", "content": "a3"}
        hist = mem.get_history("s1")
        assert hist == [
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
        ]
        assert mem.get_history("s1", limit=1) == [
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
        ]
        mem.clear("s1")
        assert key not in fake.lists
        assert mem.get_history("s1") == []

    @pytest.mark.asyncio
    async def test_custom_ttl(self):
        fake = FakeRedisClient()
        mem = ConversationMemory(storage=RedisConversationStorage(client=fake, ttl=3600))
        mem.add("s1", "u1", "a1")
        assert fake.expires.get("moa:mem:s1") == 3600

    @pytest.mark.asyncio
    async def test_falls_back_to_memory_on_connect_failure(self):
        fake = FakeRedisClient(fail_ping=True)
        mem = ConversationMemory(storage=RedisConversationStorage(client=fake))
        mem.add("s1", "hi", "hello")
        assert "moa:mem:s1" not in fake.lists
        assert mem.get_history("s1") == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        mem.clear("s1")
        assert mem.get_history("s1") == []

    @pytest.mark.asyncio
    async def test_falls_back_when_rpush_fails_after_connect(self):
        class FlakyClient(FakeRedisClient):
            async def rpush(self, key, value):
                raise ConnectionError("redis went away")

        fake = FlakyClient()
        mem = ConversationMemory(storage=RedisConversationStorage(client=fake))
        mem.add("s1", "u1", "a1")
        mem.add("s1", "u2", "a2")
        assert mem.get_history("s1", limit=1) == [
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]

    @pytest.mark.asyncio
    async def test_unknown_session_returns_empty(self):
        fake = FakeRedisClient()
        mem = ConversationMemory(storage=RedisConversationStorage(client=fake))
        assert mem.get_history("unknown") == []

    def test_storage_constructor_error_falls_back_to_memory(self):
        def broken_factory():
            raise RuntimeError("no redis available")

        mem = ConversationMemory(storage=broken_factory)
        mem.add("s1", "hi", "hello")
        assert mem.get_history("s1") == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        mem.clear("s1")
        assert mem.get_history("s1") == []

    def test_default_storage_keeps_existing_behavior(self):
        mem = ConversationMemory(max_turns=1)
        mem.add("s1", "u1", "a1")
        mem.add("s1", "u2", "a2")
        assert mem._store["s1"] == [
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        assert mem.get_history("s1") == [
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        mem.clear("s1")
        assert mem.get_history("s1") == []


class TestSharedBridge:
    def test_both_storages_share_one_bridge(self):
        hitl = RedisHitlStorage(client=FakeRedisClient())
        conv = RedisConversationStorage(client=FakeRedisClient())
        assert hitl._bridge is _SHARED_BRIDGE
        assert conv._bridge is _SHARED_BRIDGE
        assert hitl._bridge is conv._bridge

    def test_bridge_default_timeout_is_one_second(self):
        assert _SHARED_BRIDGE._timeout == 1.0
        assert RedisHitlStorage()._timeout == 1.0
        assert RedisConversationStorage()._timeout == 1.0

    def test_hitl_retry_recovers_after_temporary_connect_failure(self):
        fake = FakeRedisClient(fail_ping=True)
        storage = RedisHitlStorage(client=fake, retry_after=0.0)
        assert storage.get("k") is None
        assert storage._using_memory is True
        fake.fail_ping = False
        storage.set("k", "v")
        assert storage._using_memory is False
        assert fake.data.get("k") == "v"

    def test_conversation_retry_recovers_after_temporary_connect_failure(self):
        fake = FakeRedisClient(fail_ping=True)
        storage = RedisConversationStorage(client=fake, retry_after=0.0)
        storage.rpush("moa:mem:s1", "x")
        assert storage._using_memory is True
        assert "moa:mem:s1" in storage._memory
        fake.fail_ping = False
        storage.rpush("moa:mem:s1", "y")
        assert storage._using_memory is False
        assert fake.lists["moa:mem:s1"] == ["y"]


class TestListSessions:
    def test_list_sessions_memory_mode(self):
        mem = ConversationMemory(max_turns=2)
        mem.add("s1", "u1", "a1")
        mem.add("s2", "u2", "a2")
        assert sorted(mem.list_sessions()) == ["s1", "s2"]

    def test_list_sessions_redis_mode_unions_redis_keys(self):
        fake = FakeRedisClient()
        mem = ConversationMemory(max_turns=2, storage=RedisConversationStorage(client=fake))
        mem.add("s1", "u1", "a1")
        fake.lists["moa:mem:s2"] = [
            json.dumps({"role": "user", "content": "u"}),
            json.dumps({"role": "assistant", "content": "a"}),
        ]
        assert set(mem.list_sessions()) == {"s1", "s2"}

    def test_list_sessions_redis_mode_empty_after_clear(self):
        fake = FakeRedisClient()
        mem = ConversationMemory(max_turns=2, storage=RedisConversationStorage(client=fake))
        mem.add("s1", "u1", "a1")
        mem.clear("s1")
        assert mem.list_sessions() == []

    def test_add_maintains_local_cache_in_redis_mode(self):
        fake = FakeRedisClient()
        mem = ConversationMemory(max_turns=1, storage=RedisConversationStorage(client=fake))
        mem.add("s1", "u1", "a1")
        mem.add("s1", "u2", "a2")
        assert mem._store["s1"] == [
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        assert sorted(mem.list_sessions()) == ["s1"]
        assert mem.get_history("s1") == [
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]

    def test_get_history_backfills_store_on_first_redis_read(self):
        fake = FakeRedisClient()
        fake.lists["moa:mem:s9"] = [
            json.dumps({"role": "user", "content": "u"}),
            json.dumps({"role": "assistant", "content": "a"}),
        ]
        mem = ConversationMemory(storage=RedisConversationStorage(client=fake))
        assert "s9" not in mem._store
        assert mem.get_history("s9") == [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ]
        assert "s9" in mem._store
        assert "s9" in mem.list_sessions()
