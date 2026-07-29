from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from app.redis_state.memory_fallback import MemoryStateStore

logger = logging.getLogger("moa.redis")


@dataclass
class RedisConfig:
    url: str = "redis://localhost:6379/0"
    socket_timeout: float = 5.0
    decode_responses: bool = True
    sentinel_hosts: list[tuple[str, int]] = field(default_factory=list)
    sentinel_master: str = "mymaster"
    enable_fallback: bool = True


class RedisStateStore:
    def __init__(self, config: RedisConfig | None = None) -> None:
        self.config = config or RedisConfig()
        self._client: Redis | None = None
        self._memory: MemoryStateStore | None = None
        self._using_memory: bool = False

    async def connect(self) -> Redis | MemoryStateStore:
        if self._using_memory and self._memory:
            return self._memory

        if self._client is None:
            self._client = await self._try_connect()

        if self._client:
            try:
                await self._client.ping()
                return self._client
            except Exception:
                logger.warning("redis ping failed, reconnecting")
                self._client = await self._try_connect()
                if self._client:
                    return self._client

        return await self._fallback_to_memory()

    async def _try_connect(self) -> Redis | None:
        # 1. Try Sentinel if hosts configured.
        if self.config.sentinel_hosts:
            try:
                from redis.asyncio.sentinel import Sentinel

                sentinel = Sentinel(
                    self.config.sentinel_hosts,
                    socket_timeout=self.config.socket_timeout,
                )
                client = sentinel.master_for(
                    self.config.sentinel_master,
                    decode_responses=self.config.decode_responses,
                )
                await client.ping()
                logger.info("redis sentinel connected: %s master=%s", self.config.sentinel_hosts, self.config.sentinel_master)
                return client
            except Exception as exc:
                logger.warning("redis sentinel connect failed: %s", exc)

        # 2. Try single Redis URL.
        try:
            client = Redis.from_url(
                self.config.url,
                socket_timeout=self.config.socket_timeout,
                decode_responses=self.config.decode_responses,
                protocol=2,
            )
            await client.ping()
            logger.info("redis connected: %s", self.config.url)
            return client
        except Exception as exc:
            logger.warning("redis connect failed: %s", exc)
            return None

    async def _fallback_to_memory(self) -> MemoryStateStore:
        if not self.config.enable_fallback:
            raise ConnectionError("redis unavailable and fallback disabled")

        if self._client:
            await self._client.aclose()
            self._client = None

        self._using_memory = True
        if self._memory is None:
            self._memory = MemoryStateStore()
        logger.critical("redis fallback to in-memory store")
        return self._memory

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._memory:
            await self._memory.close()
            self._memory = None

    @property
    def is_fallback(self) -> bool:
        return self._using_memory

    # ── Delegated operations ──────────────────────────────────────────

    async def ensure_key(self, key: str, value: str, ttl: int = 86400) -> None:
        client = await self.connect()
        await client.set(key, value, ex=ttl) if hasattr(client, '__class__') else None

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

    async def eval(self, script: str, numkeys: int, *args: str) -> Any:
        client = await self.connect()
        if hasattr(client, 'eval'):
            return await client.eval(script, numkeys, *args)
        return False

    @staticmethod
    def key(session_id: str, namespace: str = "moa") -> str:
        return f"{namespace}:{session_id}"
