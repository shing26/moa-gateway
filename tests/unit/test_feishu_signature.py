from __future__ import annotations

from app.channels.feishu_signature import verify_verification_token


def test_fail_open_when_not_configured():
    assert verify_verification_token({"token": "x"}, "") is True


def test_matches_schema_1_0_body_token():
    body = {"token": "abc", "type": "url_verification", "challenge": "ch"}
    assert verify_verification_token(body, "abc") is True


def test_rejects_mismatched_body_token():
    body = {"token": "abc", "type": "url_verification", "challenge": "ch"}
    assert verify_verification_token(body, "def") is False


def test_rejects_non_dict_body():
    assert verify_verification_token([], "abc") is False
    assert verify_verification_token("abc", "abc") is False


def test_schema_2_0_token_lives_in_header_not_body():
    body = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "event_id": "e1"},
        "event": {"message": {"chat_id": "c", "msg_type": "text", "content": "{}"}},
    }
    assert verify_verification_token(body, "secret") is True
    assert verify_verification_token({}, "secret") is True


def test_encrypt_key_param_is_placeholder_no_op():
    body = {"token": "abc"}
    assert verify_verification_token(body, "abc", encrypt_key="enc") is True