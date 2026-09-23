from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("moa.redis.memory")

# 内存回退要模仿的 Lua 脚本（与真实 Redis 端同一份语义）。此前它们住在
# `lock.py`，而那个模块的 `IdempotencyLock`/`LuaLockFactory` 从未接入请求路径
# （HITL 的幂等改用挂起记录的原子认领 `pop_hitl` 解决），2026-09-23 连同
# `stack.py` 一起删除，脚本常量搬到这里——它们本来就只有本模块在用。
_ACQUIRE_LUA = """
local key = KEYS[1]
local value = ARGV[1]
local ttl = tonumber(ARGV[2])
return redis.call('SET', key, value, 'NX', 'EX', ttl)
"""

_RELEASE_LUA = """
local key = KEYS[1]
local value = ARGV[1]
local current = redis.call('GET', key)
if current == value then
    redis.call('DEL', key)
    return 1
end
return 0
"""

_EXTEND_LUA = """
local key = KEYS[1]
local value = ARGV[1]
local ttl = tonumber(ARGV[2])
local current = redis.call('GET', key)
if current == value then
    return redis.call('EXPIRE', key, ttl)
end
return 0
"""


class MemoryStateStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._expires: dict[str, float] = {}

    async def connect(self) -> MemoryStateStore:
        logger.warning("using in-memory fallback store")
        return self

    async def close(self) -> None:
        self._data.clear()
        self._lists.clear()
        self._hashes.clear()
        self._expires.clear()

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        self._expire_if_needed(key)
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value
        self._update_expiry(key, ex)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._lists.pop(key, None)
        self._hashes.pop(key, None)
        self._expires.pop(key, None)

    async def exists(self, key: str) -> bool:
        self._expire_if_needed(key)
        return key in self._data or key in self._lists or key in self._hashes

    async def expire(self, key: str, ttl: int) -> None:
        self._expire_if_needed(key)
        if key in self._data:
            self._update_expiry(key, ttl)

    async def lpush(self, key: str, value: str) -> None:
        self._lists.setdefault(key, []).insert(0, value)

    async def lpop(self, key: str) -> str | None:
        items = self._lists.get(key)
        if not items:
            return None
        return items.pop(0)

    async def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self._lists.get(key, [])
        return items[start:end] if end != -1 else items[start:]

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self._hashes.setdefault(key, {}).update(mapping)

    async def hget(self, key: str, field: str) -> str | None:
        return self._hashes.get(key, {}).get(field)

    async def eval(self, script: str, numkeys: int, *args: str) -> Any:
        if script == _ACQUIRE_LUA:
            key, value, ttl = args[0], args[1], int(args[2])
            self._expire_if_needed(key)
            if key in self._data:
                return False
            self._data[key] = value
            self._update_expiry(key, ttl)
            return True
        if script == _RELEASE_LUA:
            key, value = args[0], args[1]
            self._expire_if_needed(key)
            if self._data.get(key) != value:
                return 0
            self._data.pop(key, None)
            self._expires.pop(key, None)
            return 1
        if script == _EXTEND_LUA:
            key, value, ttl = args[0], args[1], int(args[2])
            self._expire_if_needed(key)
            if self._data.get(key) != value:
                return 0
            self._update_expiry(key, ttl)
            return 1
        return False

    def _update_expiry(self, key: str, ttl: int | None) -> None:
        if ttl is None:
            self._expires.pop(key, None)
        elif ttl <= 0:
            self._data.pop(key, None)
            self._expires.pop(key, None)
        else:
            self._expires[key] = time.monotonic() + ttl

    def _expire_if_needed(self, key: str) -> None:
        expires_at = self._expires.get(key)
        if expires_at is not None and time.monotonic() >= expires_at:
            self._data.pop(key, None)
            self._expires.pop(key, None)

    @staticmethod
    def key(session_id: str, namespace: str = "moa") -> str:
        return f"{namespace}:{session_id}"
