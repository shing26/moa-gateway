from __future__ import annotations
import asyncio
import json
import logging
import threading
from collections import defaultdict
from typing import Any, Awaitable

from redis.asyncio import Redis

logger = logging.getLogger("moa.memory")


class _SyncBridge:
    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                daemon=True,
                name="moa-redis-bridge",
            )
            self._thread.start()

    def call(self, awaitable: Awaitable[Any]) -> Any:
        self._ensure()
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result(self._timeout)


class RedisConversationStorage:
    KEY_PREFIX = "moa:mem"
    DEFAULT_TTL = 86400

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        ttl: int = DEFAULT_TTL,
        enable_fallback: bool = True,
        timeout: float = 5.0,
        client: Any | None = None,
    ) -> None:
        self.url = url
        self.ttl = ttl
        self._enable_fallback = enable_fallback
        self._timeout = timeout
        self._client = client
        self._connected = False
        self._bridge = _SyncBridge(timeout=timeout)
        self._using_memory = False
        self._memory: dict[str, list[str]] = {}

    @staticmethod
    def key(session_id: str) -> str:
        return f"{RedisConversationStorage.KEY_PREFIX}:{session_id}"

    def _resolve(self) -> Any:
        if self._using_memory:
            return None
        if not self._connected:
            try:
                client = self._bridge.call(self._connect_coro())
            except Exception as exc:
                self._fallback(exc)
                return None
            if client is None:
                self._fallback()
                return None
            self._client = client
            self._connected = True
        return self._client

    async def _connect_coro(self) -> Any:
        client = self._client
        if client is None:
            client = Redis.from_url(
                self.url,
                socket_timeout=self._timeout,
                decode_responses=True,
                protocol=2,
            )
        try:
            await client.ping()
        except Exception:
            if self._client is None:
                await client.aclose()
            return None
        logger.info("redis conversation storage connected: %s", self.url)
        return client

    def _fallback(self, exc: Exception | None = None) -> None:
        if not self._enable_fallback:
            raise ConnectionError("redis unavailable and fallback disabled") from exc
        if self._client is not None and not self._using_memory:
            try:
                self._bridge.call(self._client.aclose())
            except Exception:
                pass
            self._client = None
        self._connected = False
        self._using_memory = True
        logger.critical("redis conversation storage fallback to in-memory")

    def rpush(self, key: str, value: str) -> None:
        client = self._resolve()
        if client is None:
            self._memory.setdefault(key, []).append(value)
            return
        try:
            self._bridge.call(client.rpush(key, value))
        except Exception as exc:
            self._fallback(exc)
            self._memory.setdefault(key, []).append(value)

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        client = self._resolve()
        if client is None:
            return self._slice(self._memory.get(key, []), start, end)
        try:
            return list(self._bridge.call(client.lrange(key, start, end)))
        except Exception as exc:
            self._fallback(exc)
            return self._slice(self._memory.get(key, []), start, end)

    def ltrim(self, key: str, start: int, end: int) -> None:
        client = self._resolve()
        if client is None:
            self._memory[key] = self._slice(self._memory.get(key, []), start, end)
            return
        try:
            self._bridge.call(client.ltrim(key, start, end))
        except Exception as exc:
            self._fallback(exc)
            self._memory[key] = self._slice(self._memory.get(key, []), start, end)

    def expire(self, key: str, ttl: int) -> None:
        client = self._resolve()
        if client is None:
            return
        try:
            self._bridge.call(client.expire(key, ttl))
        except Exception as exc:
            self._fallback(exc)

    def delete(self, key: str) -> None:
        client = self._resolve()
        if client is None:
            self._memory.pop(key, None)
            return
        try:
            self._bridge.call(client.delete(key))
        except Exception as exc:
            self._fallback(exc)
            self._memory.pop(key, None)

    @staticmethod
    def _slice(items: list[str], start: int, end: int) -> list[str]:
        if not items:
            return []
        n = len(items)
        s = start if start >= 0 else max(0, n + start)
        e = n - 1 if end == -1 else (end if end >= 0 else n + end)
        return items[s : e + 1]


class ConversationMemory:
    def __init__(self, max_turns: int = 10, storage: Any = None) -> None:
        self._max = max_turns
        self._store: dict[str, list[dict[str, str]]] = defaultdict(list)
        self._storage = None
        if storage is not None:
            try:
                self._storage = storage() if callable(storage) else storage
            except Exception as exc:
                logger.warning("conversation storage unavailable, using in-memory: %s", exc)
                self._storage = None

    def add(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
        if self._storage is None:
            history = self._store[session_id]
            history.append({"role": "user", "content": user_msg})
            history.append({"role": "assistant", "content": assistant_msg})
            while len(history) > self._max * 2:
                history.pop(0)
                history.pop(0)
            return
        key = self._storage.key(session_id)
        self._storage.rpush(key, json.dumps({"role": "user", "content": user_msg}))
        self._storage.rpush(key, json.dumps({"role": "assistant", "content": assistant_msg}))
        self._storage.ltrim(key, -(self._max * 2), -1)
        self._storage.expire(key, self._storage.ttl)
        self._store.setdefault(session_id, [])

    def get_history(self, session_id: str, limit: int = 10) -> list[dict[str, str]]:
        if self._storage is None:
            return self._store.get(session_id, [])[-(limit * 2):]
        raw = self._storage.lrange(self._storage.key(session_id), -(limit * 2), -1)
        history: list[dict[str, str]] = []
        for item in raw:
            try:
                history.append(json.loads(item))
            except Exception:
                continue
        return history

    def clear(self, session_id: str) -> None:
        if self._storage is None:
            self._store.pop(session_id, None)
            return
        self._storage.delete(self._storage.key(session_id))
        self._store.pop(session_id, None)
