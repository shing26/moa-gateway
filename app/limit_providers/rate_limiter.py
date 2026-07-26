from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SlidingWindowRateLimiter:
    """In-memory sliding window rate limiter per key (user/session/IP).

    Allows max limit requests within window_seconds per unique key.
    """

    _buckets: dict[str, list[float]] = field(default_factory=dict)
    window_seconds: int = 60
    limit: int = 10

    async def check(self, key: str) -> tuple[bool, int]:
        """Returns (allowed: bool, remaining: int)."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        if key not in self._buckets:
            self._buckets[key] = []

        timestamps = self._buckets[key]
        # Prune expired entries
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)

        if len(timestamps) >= self.limit:
            return False, 0

        timestamps.append(now)
        remaining = self.limit - len(timestamps)
        return True, remaining

    def cleanup(self, max_keys: int = 10000) -> None:
        if len(self._buckets) > max_keys:
            cutoff = time.monotonic() - self.window_seconds
            self._buckets = {k: [t for t in v if t >= cutoff] for k, v in self._buckets.items()}
            self._buckets = {k: v for k, v in self._buckets.items() if v}


# Singleton for convenience
rate_limiter = SlidingWindowRateLimiter()
