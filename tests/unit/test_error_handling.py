from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from app.main import _debug_exception_handler, app
from tests.support import app_client


def test_malformed_json_returns_400_not_500() -> None:
    with app_client(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/webhook/feishu",
            # content= 传原始 body；data=<str> 已被 httpx 标记为弃用
            content="{a}",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_json"
    assert "Expecting" not in response.text


@pytest.mark.asyncio
async def test_unhandled_exception_response_is_generic() -> None:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/boom",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 80),
    }
    request = Request(scope)
    response = await _debug_exception_handler(request, RuntimeError("secret stack detail"))
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error"] == "internal_error"
    assert body["detail"] == "内部服务错误"
    assert "secret stack detail" not in response.body.decode()
    assert "RuntimeError" not in response.body.decode()
