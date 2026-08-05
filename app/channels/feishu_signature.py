from __future__ import annotations

from typing import Any


def verify_verification_token(body: dict, token: str, encrypt_key: str = "") -> bool:
    if not token:
        return True
    if not isinstance(body, dict):
        return False
    body_token = body.get("token", "")
    if body_token:
        return body_token == token
    return True


__all__ = ["verify_verification_token"]
