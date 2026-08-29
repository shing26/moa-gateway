from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

_ALLOWED = ("/health", "/healthz", "/docs", "/openapi.json")
_LARK_VERIFIED_PATHS = ("/feishu/event", "/webhook/callback")
_WEBHOOK_PREFIX = "/webhook"
_DASHBOARD_PREFIX = "/dashboard"
_DASHBOARD_STATIC = "/dashboard/static"
_DASHBOARD_USER = "admin"
_BASIC_CHALLENGE = {"WWW-Authenticate": 'Basic realm="dashboard"'}
_INSECURE_TRUTHY = frozenset({"1", "true", "yes", "on"})


def insecure_mode_enabled(raw: str | None) -> bool:
    """仅当显式配置为真值时才允许无凭据放行；其余情况一律 fail-closed。"""
    return (raw or "").strip().lower() in _INSECURE_TRUTHY


def _secret_eq(supplied: str | None, expected: str) -> bool:
    """常量时间比较，避免按字符提前返回带来的时序侧信道。"""
    return hmac.compare_digest((supplied or "").encode("utf-8"), expected.encode("utf-8"))


def _unauthorized(reason: str = "", *, challenge: bool = False) -> JSONResponse:
    body: dict[str, str] = {"error": "unauthorized"}
    if reason:
        body["reason"] = reason
    return JSONResponse(
        body,
        status_code=401,
        headers=dict(_BASIC_CHALLENGE) if challenge else None,
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """网关鉴权中间件。

    受保护端点采用 **fail-closed**：服务端未配置对应密钥时，请求直接 401
    （`reason=auth_not_configured`），不再像旧实现那样静默放行。仅当显式设置
    `GATEWAY_ALLOW_INSECURE` 为真值（本地调试）时才退回放行行为。

    `/feishu/event` 与 `/webhook/callback` 由飞书 `X-Lark-Token` 独立校验，
    不在本次 fail-closed 范围内。
    """

    def __init__(  # nosec B107
        self,
        app: Any,
        token: str = "",
        dashboard_password: str = "",
        feishu_verification_token: str = "",
        allow_insecure: bool = False,
    ) -> None:
        super().__init__(app)
        self._token = token
        self._dashboard_password = dashboard_password
        self._feishu_verification_token = feishu_verification_token
        self._allow_insecure = allow_insecure

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if path in _LARK_VERIFIED_PATHS:
            lark_token = request.headers.get("X-Lark-Token")
            if lark_token and self._feishu_verification_token and not _secret_eq(lark_token, self._feishu_verification_token):
                return _unauthorized()
            return await call_next(request)
        if self._is_allowed(path):
            return await call_next(request)
        if path.startswith(_WEBHOOK_PREFIX):
            if not self._token:
                if not self._allow_insecure:
                    return _unauthorized("auth_not_configured")
            elif not _secret_eq(request.headers.get("X-Gateway-Token"), self._token):
                return _unauthorized()
        elif path.startswith(_DASHBOARD_PREFIX) and not path.startswith(_DASHBOARD_STATIC):
            if not self._dashboard_password:
                if not self._allow_insecure:
                    return _unauthorized("auth_not_configured", challenge=True)
            elif not self._check_basic(request.headers.get("Authorization", "")):
                return _unauthorized(challenge=True)
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
        return user == _DASHBOARD_USER and _secret_eq(password, self._dashboard_password)


__all__ = ["AuthMiddleware", "insecure_mode_enabled"]
