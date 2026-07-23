from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger("moa.redis")


@dataclass(frozen=True)
class RedisConfig:
    url: str = "redis://localhost:6379/0"
    socket_timeout: float = 5.0
    decode_responses: bool = True


class RedisStateStore:
    def __init__(self, config: RedisConfig | None = None) -> None:
        self.config = config or RedisConfig()
        self._client: Redis | None = None

    async def connect(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(
                self.config.url,
                socket_timeout=self.config.socket_timeout,
                decode_responses=self.config.decode_responses,
            )
            await self._client.ping()
            logger.info("redis connected: %s", self.config.url)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ensure_key(self, key: str, value: str, ttl: int = 86400) -> None:
        client = await self.connect()
        await client.set(key, value, ex=ttl, nx=True)

    async def get(self, key: str) -> str | None:
        client = await self.connect()
        return await client.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        client = await self.connect()
        if ttl is not None:
            await client.set(key, value, ex=ttl)
        else:
            await client.set(key, value)

    async def delete(self, key: str) -> None:
        client = await self.connect()
        await client.delete(key)

    async def lpush(self, key: str, value: str) -> None:
        client = await self.connect()
        await client.lpush(key, value)

    async def lpop(self, key: str) -> str | None:
        client = await self.connect()
        return await client.lpop(key)

    async def llen(self, key: str) -> int:
        client = await self.connect()
        return int(await client.llen(key) or 0)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        client = await self.connect()
        return list(await client.lrange(key, start, end))

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        client = await self.connect()
        await client.hset(key, mapping=mapping)

    async def hget(self, key: str, field: str) -> str | None:
        client = await self.connect()
        return await client.hget(key, field)

    @staticmethod
    def key(session_id: str, namespace: str = "moa") -> str:
        return f"{namespace}:{session_id}"
