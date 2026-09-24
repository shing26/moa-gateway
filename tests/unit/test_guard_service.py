from __future__ import annotations

import logging

from app.config import settings
from app.guard.guard_service import guard_service
from app.guard.rbac import GuardianAction, Role


class TestEvaluateOutput:
    def test_deny_wins_over_review(self):
        verdict, policy_ids = guard_service.evaluate_output("服务器地址 10.0.0.1, 优惠价只要 99 元")
        assert verdict.action == GuardianAction.DENY
        assert "policy.security.internal_ip" in verdict.reason
        assert set(policy_ids) == {
            "policy.security.internal_ip",
            "policy.compliance.no_price_commitment",
        }

    def test_review_for_review_only_hits(self):
        verdict, policy_ids = guard_service.evaluate_output("优惠价只要 99 元")
        assert verdict.action == GuardianAction.REVIEW
        assert policy_ids == ("policy.compliance.no_price_commitment",)

    def test_deny_for_deny_only_hits(self):
        verdict, policy_ids = guard_service.evaluate_output("AKIA1234567890ABCDEF")
        assert verdict.action == GuardianAction.DENY
        assert policy_ids == ("policy.security.secret_leak",)

    def test_allow_when_no_policy_hit(self):
        verdict, policy_ids = guard_service.evaluate_output("今天天气不错, 适合写代码")
        assert verdict.action == GuardianAction.ALLOW
        assert policy_ids == ()

    def test_policy_ids_match_aggregated_hits(self):
        verdict, policy_ids = guard_service.evaluate_output("内网 10.0.0.1 的密钥 sk-abc123XYZuvw4567890abcdef")
        assert verdict.action == GuardianAction.DENY
        assert set(policy_ids) == {
            "policy.security.internal_ip",
            "policy.security.secret_leak",
        }

    def test_default_role_resolved_from_settings(self, monkeypatch):
        # 2026-09-24 收编：MOA_DEFAULT_ROLE 的唯一读取点是 app/config.py，
        # 测试改 patch settings（env 直读已删除，setenv 不再有效果）。
        monkeypatch.setattr(settings, "default_role", "admin")
        verdict, _ = guard_service.evaluate_output("今天天气不错")
        assert verdict.role == Role.ADMIN

    def test_default_role_falls_back_to_operator(self):
        # conftest 把 MOA_DEFAULT_ROLE 置空 → settings.default_role 是默认值，
        # 不依赖开发者 .env（非 hermetic 防线与其它凭据同一套）。
        verdict, _ = guard_service.evaluate_output("今天天气不错")
        assert verdict.role == Role.OPERATOR

    def test_explicit_role_overrides_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "default_role", "viewer")
        verdict, _ = guard_service.evaluate_output("今天天气不错", role=Role.ADMIN)
        assert verdict.role == Role.ADMIN

    def test_intent_param_is_accepted(self):
        verdict, policy_ids = guard_service.evaluate_output("优惠价只要 99 元", intent="assistant")
        assert verdict.action == GuardianAction.REVIEW
        assert policy_ids == ("policy.compliance.no_price_commitment",)


def test_evaluate_does_not_log_warning_per_request(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="moa.guard.service"):
        guard_service.evaluate("coder", "coding", {"role": "operator"}, hitl_enabled=False)
    assert not [r for r in caplog.records if "evaluate agent=" in r.getMessage()]
