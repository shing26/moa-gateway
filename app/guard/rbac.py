from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class Role(str, Enum):
    """RBAC roles with ascending privilege level."""
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True)
class Permission:
    """A single permission action on a resource."""
    action: str
    resource: str

    def match(self, action: str, resource: str) -> bool:
        return self.action == action and self.resource == resource


# @dataclass removed - breaks str(Enum) ==
class GuardianAction(str, Enum):
    """Result of a guard evaluation."""
    ALLOW = "allow"
    REVIEW = "review"   # requires human-in-the-loop approval
    DENY = "deny"


@dataclass(frozen=True)
class GuardVerdict:
    action: GuardianAction
    reason: str
    role: Role | None = None


# 鈹€鈹€ built-in permission sets 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

DEFAULT_ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.VIEWER: {
        Permission("read", "agent_output"),
    },
    Role.OPERATOR: {
        Permission("read", "agent_output"),
        Permission("read", "audit_log"),
        Permission("execute", "agent"),
    },
    Role.ADMIN: {
        Permission("read", "agent_output"),
        Permission("read", "audit_log"),
        Permission("execute", "agent"),
        Permission("manage", "guard"),
        Permission("manage", "prompt"),
    },
}

SENSITIVE_RESOURCES: set[str] = {"guard", "prompt"}


def get_role_permissions(role: Role) -> set[Permission]:
    return DEFAULT_ROLE_PERMISSIONS.get(role, set())


def has_permission(role: Role | None, action: str, resource: str) -> bool:
    if role is None:
        return False
    return any(p.match(action, resource) for p in get_role_permissions(role))


def resolve_role(slot: dict[str, Any]) -> Role:
    """Resolve the effective role from an agent slot dict.
    Falls back to VIEWER if the slot key is absent or invalid."""
    raw = slot.get("role", "viewer")
    try:
        return Role(raw.lower())
    except ValueError:
        return Role.VIEWER

