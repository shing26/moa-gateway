from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

# Lua scripts for idempotent distributed lock operations.
# Each script accepts KEYS[1] = lock key, ARGV[1] = lock value, ARGV[2] = TTL (seconds, for acquire/extend).
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


@dataclass
class IdempotencyLock:
    """A Lua-backed distributed lock that is idempotent: re-acquiring the same
    lock with the same value is a no-op (returns True) rather than a failure."""

    redis: Any
    key: str
    value: str = ""
    ttl: int = 60
    _held: bool = False

    def __post_init__(self) -> None:
        if not self.value:
            self.value = uuid.uuid4().hex

    async def acquire(self) -> bool:
        # Idempotent: if already held with same value, treat as success.
        current = await self.redis.get(self.key)
        if current == self.value:
            self._held = True
            return True
        result = await self.redis.eval(_ACQUIRE_LUA, 1, self.key, self.value, str(self.ttl))
        self._held = result is True or result == b"OK" or result == "OK"
        return self._held

    async def release(self) -> bool:
        result = await self.redis.eval(_RELEASE_LUA, 1, self.key, self.value)
        self._held = False
        return bool(result)

    async def extend(self, ttl: int | None = None) -> bool:
        t = ttl if ttl is not None else self.ttl
        result = await self.redis.eval(_EXTEND_LUA, 1, self.key, self.value, str(t))
        return bool(result)

    @property
    def held(self) -> bool:
        return self._held


class LuaLockFactory:
    """Factory that creates IdempotencyLock instances bound to a redis client."""

    def __init__(self, redis: Any, default_ttl: int = 60) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    def lock(self, name: str, ttl: int | None = None, value: str = "") -> IdempotencyLock:
        return IdempotencyLock(
            redis=self._redis,
            key=f"moa:lock:{name}",
            value=value,
            ttl=ttl if ttl is not None else self._default_ttl,
        )
