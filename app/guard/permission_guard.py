from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str


class PermissionGuard(Protocol):
    async def check(self, agent_name: str, slot: dict[str, object]) -> GuardDecision: ...


class FailClosedPermissionGuard:
    async def check(self, agent_name: str, slot: dict[str, object]) -> GuardDecision:
        required_key = "required_permissions"
        required_permissions: list[str] | None = slot.get(required_key) if isinstance(slot, dict) else None
        if required_permissions is None:
            return GuardDecision(allowed=False, reason="missing_payload_schema")
        for permission in required_permissions:
            if not isinstance(permission, str) or not permission.strip():
                return GuardDecision(allowed=False, reason="invalid_permission_entry")
        return GuardDecision(allowed=True, reason="ok")
    
    async def check_intent(self, agent_name: str, intent: str, payload: dict[str, object]) -> GuardDecision:
        sensitive_intents = {"write_file", "execute_command", "send_message"}
        if intent in sensitive_intents:
            return GuardDecision(allowed=False, reason=f"sensitive_intent:{intent}")
        return GuardDecision(allowed=True, reason="ok")
