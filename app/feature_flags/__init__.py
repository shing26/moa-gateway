from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("moa.feature_flags")


@dataclass
class FeatureFlagClient:
    """Dynamic feature flag client with dict backing and local TTL cache.

    - Uses an in-memory dict (can be swapped for Redis).
    - Falls back to environment variables (MOA_FLAG_<NAME>).
    - Local TTL cache (5s default) avoids repeated lookups.
    """

    _store: dict[str, Any] = field(default_factory=dict)
    _cache: dict[str, tuple[Any, float]] = field(default_factory=dict)
    _cache_ttl: float = 5.0

    @staticmethod
    def flag_key(name: str) -> str:
        return f"moa:flag:{name}"

    async def get(self, name: str, default: Any = False) -> Any:
        now = time.monotonic()

        if name in self._cache:
            value, expires = self._cache[name]
            if now < expires:
                return value

        result = default
        key = self.flag_key(name)
        if key in self._store:
            result = self._parse_value(self._store[key])

        env_key = f"MOA_FLAG_{name.upper().replace('.', '_')}"
        env_val = os.environ.get(env_key)
        if env_val is not None:
            result = self._parse_value(env_val)

        self._cache[name] = (result, now + self._cache_ttl)
        return result

    async def set(self, name: str, value: Any) -> None:
        self._store[self.flag_key(name)] = str(value)
        self._cache.pop(name, None)

    async def delete(self, name: str) -> None:
        self._store.pop(self.flag_key(name), None)
        self._cache.pop(name, None)

    def invalidate(self, name: str | None = None) -> None:
        if name:
            self._cache.pop(name, None)
        else:
            self._cache.clear()

    def seed(self, flags: dict[str, Any]) -> None:
        for name, value in flags.items():
            self._store[self.flag_key(name)] = str(value)

    @staticmethod
    def _parse_value(raw: str) -> Any:
        raw = raw.strip()
        if raw.lower() in ("true", "1", "yes"):
            return True
        if raw.lower() in ("false", "0", "no"):
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw


# Default flag values.
DEFAULT_FLAGS: dict[str, Any] = {
    "guard.enabled": True,
    "guard.hitl": True,
    "evaluator.enabled": True,
    "evaluator.skip_ast": False,
    "canary.enabled": False,
    "canary.traffic_pct": 10,
}

__all__ = ["DEFAULT_FLAGS", "FeatureFlagClient"]
