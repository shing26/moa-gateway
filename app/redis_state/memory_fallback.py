from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("moa.redis.memory")


class MemoryStateStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}
        self._hashes: dict[str, dict[str, str]] = {}

    async def connect(self) -> MemoryStateStore:
        logger.warning("using in-memory fallback store")
        return self

    async def close(self) -> None:
        self._data.clear()
        self._lists.clear()
        self._hashes.clear()

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._lists.pop(key, None)
        self._hashes.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._data or key in self._lists or key in self._hashes

    async def expire(self, key: str, ttl: int) -> None:
        pass

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
        return False

    @staticmethod
    def key(session_id: str, namespace: str = "moa") -> str:
        return f"{namespace}:{session_id}"
