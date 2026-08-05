from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.auth import AuthMiddleware


def build_client(
    token: str = "",
    dashboard_password: str = "",
    feishu_verification_token: str = "",
) -> TestClient:
    inner = FastAPI()

    @inner.get("/health")
    async def health():
        return {"status": "ok"}

    @inner.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @inner.post("/feishu/event")
    async def feishu_event():
        return {"ok": True}

    @inner.post("/webhook/test")
    async def webhook_test():
        return {"ok": True}

    @inner.post("/webhook/callback")
    async def webhook_callback():
        return {"ok": True}

    @inner.get("/dashboard")
    async def dashboard():
        return {"ok": True}

    @inner.get("/dashboard/static/app.css")
    async def static_asset():
        return {"ok": True}

    inner.add_middleware(
        AuthMiddleware,
        token=token,
        dashboard_password=dashboard_password,
        feishu_verification_token=feishu_verification_token,
    )
    return TestClient(inner)


def test_fail_open_when_no_secrets():
    with build_client() as client:
        assert client.post("/webhook/test").status_code == 200
        assert client.get("/dashboard").status_code == 200


def test_webhook_requires_token_when_configured():
    with build_client(token="secret") as client:
        assert client.post("/webhook/test").status_code == 401
        assert client.post("/webhook/test").json() == {"error": "unauthorized"}
        wrong = client.post("/webhook/test", headers={"X-Gateway-Token": "wrong"})
        assert wrong.status_code == 401
        ok = client.post("/webhook/test", headers={"X-Gateway-Token": "secret"})
        assert ok.status_code == 200


def test_webhook_callback_exempt_from_token():
    with build_client(token="secret") as client:
        assert client.post("/webhook/callback").status_code == 200


def test_dashboard_requires_basic_auth_when_configured():
    with build_client(dashboard_password="pw") as client:
        res = client.get("/dashboard")
        assert res.status_code == 401
        assert res.json() == {"error": "unauthorized"}
        assert res.headers.get("WWW-Authenticate") == 'Basic realm="dashboard"'
        assert client.get("/dashboard", auth=("admin", "wrong")).status_code == 401
        assert client.get("/dashboard", auth=("admin", "pw")).status_code == 200


def test_dashboard_basic_auth_manual_header():
    cred = base64.b64encode(b"admin:pw").decode()
    with build_client(dashboard_password="pw") as client:
        assert client.get("/dashboard", headers={"Authorization": f"Basic {cred}"}).status_code == 200


def test_dashboard_rejects_non_basic_scheme():
    with build_client(dashboard_password="pw") as client:
        res = client.get("/dashboard", headers={"Authorization": "Bearer abc"})
        assert res.status_code == 401
        assert res.headers.get("WWW-Authenticate") == 'Basic realm="dashboard"'


def test_dashboard_static_exempt_from_basic_auth():
    with build_client(dashboard_password="pw") as client:
        assert client.get("/dashboard/static/app.css").status_code == 200


def test_allow_list_always_passes():
    with build_client(token="secret", dashboard_password="pw") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.post("/feishu/event").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_feishu_event_requires_lark_token_when_configured():
    with build_client(feishu_verification_token="lark-secret") as client:
        no_header = client.post("/feishu/event", json={"schema": "2.0", "header": {}})
        assert no_header.status_code == 401
        assert no_header.json() == {"error": "unauthorized"}
        wrong = client.post("/feishu/event", headers={"X-Lark-Token": "wrong"})
        assert wrong.status_code == 401
        ok = client.post("/feishu/event", headers={"X-Lark-Token": "lark-secret"})
        assert ok.status_code == 200


def test_feishu_event_fail_open_when_not_configured():
    with build_client() as client:
        assert client.post("/feishu/event").status_code == 200


def test_webhook_callback_requires_lark_token_when_configured():
    with build_client(token="gateway-secret", feishu_verification_token="lark-secret") as client:
        no_header = client.post("/webhook/callback")
        assert no_header.status_code == 401
        assert no_header.json() == {"error": "unauthorized"}
        gateway_only = client.post(
            "/webhook/callback", headers={"X-Gateway-Token": "gateway-secret"}
        )
        assert gateway_only.status_code == 401
        wrong = client.post("/webhook/callback", headers={"X-Lark-Token": "wrong"})
        assert wrong.status_code == 401
        ok = client.post("/webhook/callback", headers={"X-Lark-Token": "lark-secret"})
        assert ok.status_code == 200


def test_webhook_callback_fail_open_when_not_configured():
    with build_client(token="gateway-secret") as client:
        assert client.post("/webhook/callback").status_code == 200
