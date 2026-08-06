from __future__ import annotations

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

    def test_default_role_resolved_from_env(self, monkeypatch):
        monkeypatch.setenv("MOA_DEFAULT_ROLE", "admin")
        verdict, _ = guard_service.evaluate_output("今天天气不错")
        assert verdict.role == Role.ADMIN

    def test_default_role_falls_back_to_operator(self, monkeypatch):
        monkeypatch.delenv("MOA_DEFAULT_ROLE", raising=False)
        verdict, _ = guard_service.evaluate_output("今天天气不错")
        assert verdict.role == Role.OPERATOR

    def test_explicit_role_overrides_env(self, monkeypatch):
        monkeypatch.setenv("MOA_DEFAULT_ROLE", "viewer")
        verdict, _ = guard_service.evaluate_output("今天天气不错", role=Role.ADMIN)
        assert verdict.role == Role.ADMIN

    def test_intent_param_is_accepted(self):
        verdict, policy_ids = guard_service.evaluate_output("优惠价只要 99 元", intent="assistant")
        assert verdict.action == GuardianAction.REVIEW
        assert policy_ids == ("policy.compliance.no_price_commitment",)
