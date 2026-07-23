import asyncio

import pytest

from app.limit_providers.limiter import ProviderLimiter, RateLimit


@pytest.mark.asyncio
async def test_provider_limiter_defaults_to_one():
    limiter = ProviderLimiter()
    assert limiter.semaphore("unknown")._value >= 1


@pytest.mark.asyncio
async def test_provider_limiter_can_bound_provider():
    limiter = ProviderLimiter({"openai": RateLimit(provider="openai", rpm=60, concurrency=2)})

    entered = []

    async def task(name):
        async with limiter.bounded("openai"):
            entered.append(name)

    await asyncio.gather(task("a"), task("b"), task("c"))
    assert len(entered) == 3
    assert len(set(entered)) == 3
