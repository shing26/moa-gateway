"""飞书回调 token 校验。

**注意：这组用例在 2026-09-23 被刻意翻转。** 此前它们钉的是 fail-open：
- ``test_fail_open_when_not_configured``：未配置凭据 → 放行
- ``test_schema_2_0_token_lives_in_header_not_body``：v2 的 token 在 ``header.token``，
  顶层取不到 → **放行**（当时的结论是"v2 不校验"）

那是刻意的决策，不是疏忽。但它的后果是：**审批这个端点谁都能 POST** ——
知道 session/trace 就能批准一条真实业务的输出。所以现在改成与 ``AuthMiddleware``
一致的 fail-closed：配了就必须对（v1 顶层 / v2 ``header.token``），没配则只在显式
``GATEWAY_ALLOW_INSECURE`` 时放行。翻转的理由是"给门装锁"，不是"更严格更好看"。
"""

from __future__ import annotations

import pytest

from app.channels.feishu_signature import extract_token, verify_verification_token

TOKEN = "ErQAsNVakL9ck3TAb70dTf2zWhikuwcL"


# ── 配了凭据：必须比对（此前 v2 直接放行，这是本次翻转的核心）────────────

def test_v1_top_level_token_matches() -> None:
    body = {"token": TOKEN, "type": "url_verification", "challenge": "ch"}
    assert verify_verification_token(body, TOKEN) is True


def test_v2_header_token_matches() -> None:
    """v2 的 token 在 header.token —— 此前取不到就放行，现在必须真的比对。"""
    body = {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "event_id": "e1", "token": TOKEN},
        "event": {"action": {"value": {"action": "approve"}}},
    }
    assert verify_verification_token(body, TOKEN) is True


@pytest.mark.parametrize(
    "body",
    [
        {"schema": "2.0", "header": {"event_id": "e1"}, "event": {}},   # v2 无 token
        {"type": "card_action", "open_chat_id": "c"},                   # v1 无 token
        {},                                                             # 空体
    ],
)
def test_configured_token_absent_is_rejected(body: dict) -> None:
    """配了凭据却取不到 token → **拒绝**（此前一律放行）。"""
    assert verify_verification_token(body, TOKEN) is False


def test_wrong_token_is_rejected() -> None:
    assert verify_verification_token({"token": "nope"}, TOKEN) is False
    assert verify_verification_token({"header": {"token": "nope"}}, TOKEN) is False


def test_non_dict_body_is_rejected() -> None:
    assert verify_verification_token([], TOKEN) is False
    assert verify_verification_token("abc", TOKEN) is False


# ── 未配凭据：与 AuthMiddleware 同样 fail-closed ────────────────────────

def test_unconfigured_is_rejected_by_default() -> None:
    assert verify_verification_token({"token": "x"}, "") is False


def test_unconfigured_allows_only_in_explicit_insecure_mode() -> None:
    assert verify_verification_token({"token": "x"}, "", allow_insecure=True) is True


# ── token 提取 ──────────────────────────────────────────────────────────

def test_extract_token_prefers_top_level_then_header() -> None:
    assert extract_token({"token": "a", "header": {"token": "b"}}) == "a"
    assert extract_token({"header": {"token": "b"}}) == "b"
    assert extract_token({"header": "not-a-dict"}) == ""
    assert extract_token({"header": {"token": ""}}) == ""
