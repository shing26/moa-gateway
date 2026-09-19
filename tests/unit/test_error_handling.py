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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message", "expected_status"),
    [
        ("rate_limited", "slow down", 429),
        ("validation_error", "bad input", 400),
        ("hitl_request_not_found", "gone", 404),
        ("agent_failed", "", 500),
    ],
)
async def test_moa_error_maps_to_its_http_status(code, message, expected_status) -> None:
    from app.models.errors import MoaError

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
    response = await _debug_exception_handler(request, MoaError(code, message))
    assert response.status_code == expected_status
    body = json.loads(response.body)
    assert body["error"] == code
    assert body["message"] == (message or code)


@pytest.mark.asyncio
async def test_invalid_state_transition_maps_to_structured_500() -> None:
    from app.fsm.state_machine import InvalidStateTransitionException

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
    response = await _debug_exception_handler(
        request, InvalidStateTransitionException("INIT + HUMAN_APPROVED")
    )
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error"] == "invalid_state_transition"
    assert "INIT + HUMAN_APPROVED" not in response.body.decode()


def test_http_status_for_unknown_code_falls_back_to_500() -> None:
    from app.models.errors import http_status_for

    assert http_status_for("no_such_code") == 500
    assert http_status_for("budget_exceeded") == 429
