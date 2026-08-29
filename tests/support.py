"""带网关凭据的 TestClient 工厂。

鉴权改为 fail-closed 之后，``/webhook/*`` 需要 ``X-Gateway-Token``、
``/dashboard`` 需要 Basic 凭据。业务测试应显式用 ``app_client(app)`` 建客户端，
让"这个客户端是已鉴权的"在代码里可见——而不是靠 monkeypatch 把鉴权绕掉。

鉴权本身的正反用例集中在 ``tests/unit/test_auth.py``，包括未带凭据必须 401
的负向用例，避免出现"测试恰好掩盖了鉴权缺陷"的情况。

凭据从环境变量回读（由 ``tests/conftest.py`` 注入），与中间件启动时读到的
是同一份值。
"""

from __future__ import annotations

import base64
import os
from typing import Any

from fastapi.testclient import TestClient

DASHBOARD_USER = "admin"


def gateway_token() -> str:
    return os.environ.get("WEBHOOK_AUTH_TOKEN", "")


def dashboard_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def basic_auth_header(user: str = DASHBOARD_USER, password: str | None = None) -> str:
    raw = f"{user}:{dashboard_password() if password is None else password}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def auth_headers() -> dict[str, str]:
    """网关鉴权所需的全部请求头（webhook 令牌 + dashboard Basic）。"""
    return {"X-Gateway-Token": gateway_token(), "Authorization": basic_auth_header()}


def app_client(app: Any, **kwargs: Any) -> TestClient:
    """构造已携带网关凭据的 TestClient。

    ``headers`` 可继续传入，调用方自定义的同名头优先。
    其余关键字参数（如 ``raise_server_exceptions=False``）原样透传。
    """
    headers = {**auth_headers(), **(kwargs.pop("headers", None) or {})}
    return TestClient(app, headers=headers, **kwargs)


__all__ = [
    "DASHBOARD_USER",
    "app_client",
    "auth_headers",
    "basic_auth_header",
    "dashboard_password",
    "gateway_token",
]
