from __future__ import annotations

import pytest

from app.guard.rbac import (
    GuardianAction,
    GuardVerdict,
    Role,
    get_role_permissions,
    has_permission,
    resolve_role,
)
from app.guard.guard_service import GuardService, guard_service


class TestRbacModel:
    def test_resolve_role_defaults_to_viewer(self):
        assert resolve_role({}) == Role.VIEWER

    def test_resolve_role_unknown_falls_back_to_viewer(self):
        assert resolve_role({"role": "superadmin"}) == Role.VIEWER

    def test_resolve_role_parses_valid_roles(self):
        assert resolve_role({"role": "admin"}) == Role.ADMIN
        assert resolve_role({"role": "OPERATOR"}) == Role.OPERATOR
        assert resolve_role({"role": "Viewer"}) == Role.VIEWER

    def test_admin_has_manage_guard_permission(self):
        assert has_permission(Role.ADMIN, "manage", "guard") is True

    def test_viewer_cannot_manage_guard(self):
        assert has_permission(Role.VIEWER, "manage", "guard") is False

    def test_operator_can_execute_agent(self):
        assert has_permission(Role.OPERATOR, "execute", "agent") is True

    def test_viewer_cannot_execute_agent(self):
        assert has_permission(Role.VIEWER, "execute", "agent") is False

    def test_none_role_returns_false(self):
        assert has_permission(None, "read", "agent_output") is False

    def test_get_role_permissions_includes_correct_set(self):
        admin_perms = get_role_permissions(Role.ADMIN)
        assert any(p.match("manage", "guard") for p in admin_perms)
        assert any(p.match("execute", "agent") for p in admin_perms)


class TestGuardService:
    def test_allow_normal_intent(self):
        verdict = guard_service.evaluate("general", "assistant", {"role": "viewer"})
        assert verdict.action == GuardianAction.ALLOW
        assert verdict.reason == "ok"

    def test_review_write_file_intent_when_hitl_enabled(self):
        verdict = guard_service.evaluate("coder", "write_file", {"role": "operator"}, hitl_enabled=True)
        assert verdict.action == GuardianAction.REVIEW
        assert "human approval" in verdict.reason

    def test_deny_write_file_with_viewer_role(self):
        verdict = guard_service.evaluate("coder", "write_file", {"role": "viewer"}, hitl_enabled=True)
        assert verdict.action == GuardianAction.DENY

    def test_deny_guard_resource_with_operator(self):
        verdict = guard_service.evaluate(
            "admin_agent", "manage", {"role": "operator", "resource": "guard"}, hitl_enabled=True
        )
        assert verdict.action == GuardianAction.DENY

    def test_review_not_triggered_when_hitl_disabled(self):
        verdict = guard_service.evaluate("coder", "write_file", {"role": "operator"}, hitl_enabled=False)
        assert verdict.action == GuardianAction.ALLOW

    def test_verdict_includes_role(self):
        verdict = guard_service.evaluate("agent", "assistant", {"role": "admin"})
        assert verdict.role == Role.ADMIN

    @pytest.mark.asyncio
    async def test_check_async_wrapper(self):
        verdict = await guard_service.check("agent", {"intent": "assistant", "role": "viewer"})
        assert verdict.action == GuardianAction.ALLOW
        assert verdict.role == Role.VIEWER


class TestFailClosedGuard:
    @pytest.mark.asyncio
    async def test_allows_basic_payload(self):
        from app.guard.permission_guard import FailClosedPermissionGuard
        guard = FailClosedPermissionGuard()
        decision = await guard.check("assistant", {"required_permissions": ["read"]})
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_blocks_missing_schema(self):
        from app.guard.permission_guard import FailClosedPermissionGuard
        guard = FailClosedPermissionGuard()
        decision = await guard.check("assistant", {})
        assert decision.allowed is False
