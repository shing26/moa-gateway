from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("moa.limits")


@dataclass(frozen=True)
class RateLimit:
    provider: str
    rpm: int
    concurrency: int = 1


class ProviderLimiter:
    def __init__(self, limits: dict[str, RateLimit] | None = None) -> None:
        self.limits = limits or {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def semaphore(self, provider: str) -> asyncio.Semaphore:
        if provider not in self._semaphores:
            limit = self.limits.get(provider)
            concurrency = limit.concurrency if limit else 1
            self._semaphores[provider] = asyncio.Semaphore(concurrency)
        return self._semaphores[provider]

    async def acquire(self, provider: str) -> None:
        await self.semaphore(provider).acquire()

    def release(self, provider: str) -> None:
        sem = self._semaphores.get(provider)
        if sem is not None and sem.locked():
            sem.release()

    @contextlib.asynccontextmanager
    async def bounded(self, provider: str) -> Any:
        await self.acquire(provider)
        try:
            yield
        finally:
            self.release(provider)
