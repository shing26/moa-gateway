from __future__ import annotations

import json

import pytest

from app.engine import HitlRequest, RedisHitlStorage, SessionStore
from app.memory import ConversationMemory, RedisConversationStorage


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
        assert "moa:hitl:sess-1" in fake.data
        assert fake.expires.get("moa:hitl:sess-1") == RedisHitlStorage.DEFAULT_TTL
        got = store.get_hitl("sess-1")
        assert got == req
        store.remove_hitl("sess-1")
        assert store.get_hitl("sess-1") is None
        assert "moa:hitl:sess-1" not in fake.data

    @pytest.mark.asyncio
    async def test_custom_ttl(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake, ttl=120))
        store.store_hitl("sess-1", _hitl())
        assert fake.expires.get("moa:hitl:sess-1") == 120

    @pytest.mark.asyncio
    async def test_key_format(self):
        assert RedisHitlStorage.key("sess-1") == "moa:hitl:sess-1"
        assert RedisConversationStorage.key("sess-1") == "moa:mem:sess-1"

    @pytest.mark.asyncio
    async def test_json_payload_roundtrip_preserves_fields(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        req = _hitl(agent_output="secret", target="chat_999")
        store.store_hitl("sess-1", req)
        payload = json.loads(fake.data["moa:hitl:sess-1"])
        assert payload == {
            "session_id": "sess-1",
            "trace_id": "trace-1",
            "agent_output": "secret",
            "intent": "write_file",
            "agent_name": "coder",
            "channel": "feishu",
            "target": "chat_999",
        }
        assert store.get_hitl("sess-1") == req

    @pytest.mark.asyncio
    async def test_corrupt_payload_returns_none(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        fake.data["moa:hitl:sess-1"] = "not-json"
        assert store.get_hitl("sess-1") is None

    @pytest.mark.asyncio
    async def test_clear_all(self):
        fake = FakeRedisClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        store.store_hitl("s1", _hitl(session_id="s1"))
        store.store_hitl("s2", _hitl(session_id="s2"))
        store.clear_all()
        assert "moa:hitl:s1" not in fake.data
        assert "moa:hitl:s2" not in fake.data
        assert store.get_hitl("s1") is None
        assert store.get_hitl("s2") is None

    @pytest.mark.asyncio
    async def test_falls_back_to_memory_on_connect_failure(self):
        fake = FakeRedisClient(fail_ping=True)
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        req = _hitl()
        store.store_hitl("sess-1", req)
        assert "moa:hitl:sess-1" not in fake.data
        assert store.get_hitl("sess-1") == req
        store.remove_hitl("sess-1")
        assert store.get_hitl("sess-1") is None

    @pytest.mark.asyncio
    async def test_falls_back_when_set_fails_after_connect(self):
        class FlakyClient(FakeRedisClient):
            async def set(self, key, value, ex=None):
                raise ConnectionError("redis went away")

        fake = FlakyClient()
        store = SessionStore(storage=RedisHitlStorage(client=fake))
        store.store_hitl("sess-1", _hitl())
        assert store.get_hitl("sess-1") == _hitl()
        store.remove_hitl("sess-1")
        assert store.get_hitl("sess-1") is None

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
        assert store.get_hitl("sess-1") == req
        store.remove_hitl("sess-1")
        assert store.get_hitl("sess-1") is None

    def test_default_storage_is_memory(self):
        store = SessionStore()
        req = _hitl()
        store.store_hitl("sess-1", req)
        assert store._pending_hitl["sess-1"] is req
        assert store.get_hitl("sess-1") is req
        store.clear_all()
        assert store.get_hitl("sess-1") is None


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
