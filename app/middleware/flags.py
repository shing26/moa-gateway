from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.feature_flags import FeatureFlagClient


class FeatureFlagMiddleware(BaseHTTPMiddleware):
    """Injects feature flag state into request.state.flags for every request.

    Usage in routes:
        enabled = request.state.flags.get("guard.enabled", True)
    """

    def __init__(self, app: Any, client: FeatureFlagClient | None = None) -> None:
        super().__init__(app)
        self._client = client or FeatureFlagClient()

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request.state.flags = FlagSnapshot(self._client)
        return await call_next(request)


class FlagSnapshot:
    """Lazy-evaluated snapshot of flag state tied to a single request."""

    def __init__(self, client: FeatureFlagClient) -> None:
        self._client = client
        self._resolved: dict[str, Any] = {}

    async def get(self, name: str, default: Any = False) -> Any:
        if name not in self._resolved:
            self._resolved[name] = await self._client.get(name, default)
        return self._resolved[name]

    def get_sync(self, name: str, default: Any = False) -> Any:
        return self._resolved.get(name, default)

    def snapshot(self) -> dict[str, Any]:
        return dict(self._resolved)


__all__ = ["FeatureFlagMiddleware", "FlagSnapshot"]
