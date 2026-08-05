from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_ALLOWED = ("/health", "/healthz", "/feishu/event", "/docs", "/openapi.json")
_WEBHOOK_PREFIX = "/webhook"
_WEBHOOK_EXEMPT = "/webhook/callback"
_DASHBOARD_PREFIX = "/dashboard"
_DASHBOARD_STATIC = "/dashboard/static"
_DASHBOARD_USER = "admin"


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, token: str = "", dashboard_password: str = "") -> None:
        super().__init__(app)
        self._token = token
        self._dashboard_password = dashboard_password

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if self._is_allowed(path):
            return await call_next(request)
        if path.startswith(_WEBHOOK_PREFIX) and not path.startswith(_WEBHOOK_EXEMPT):
            if self._token and request.headers.get("X-Gateway-Token") != self._token:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        elif path.startswith(_DASHBOARD_PREFIX) and not path.startswith(_DASHBOARD_STATIC):
            if self._dashboard_password and not self._check_basic(request.headers.get("Authorization", "")):
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="dashboard"'},
                )
        return await call_next(request)

    @staticmethod
    def _is_allowed(path: str) -> bool:
        return any(path == entry or path.startswith(entry + "/") for entry in _ALLOWED)

    def _check_basic(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        user, _, password = decoded.partition(":")
        return user == _DASHBOARD_USER and password == self._dashboard_password


__all__ = ["AuthMiddleware"]
