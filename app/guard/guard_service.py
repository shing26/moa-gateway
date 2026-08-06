from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from app.guard.policies import policy_engine
from app.guard.rbac import (
    GuardianAction,
    GuardVerdict,
    Role,
    has_permission,
    resolve_role,
)

logger = logging.getLogger("moa.guard.service")

# ── Intents that always require human review ──────────────────────────
HITL_INTENTS: set[str] = {"write_file", "execute_command", "send_message", "delete_resource", "execute_code"}

# ── Resources that require elevated roles ─────────────────────────────
SENSITIVE_RESOURCES: set[str] = {"guard", "prompt", "audit_log", "user_data"}


@dataclass
class GuardService:
    """Three-level guard: ALLOW / REVIEW / DENY.

    - ALLOW  → agent output is safe, proceed directly.
    - REVIEW → output needs HITL approval via Feishu card.
    - DENY   → output is blocked, return error message.
    """

    def evaluate(
        self,
        agent_name: str,
        intent: str,
        payload: dict[str, Any],
        *,
        hitl_enabled: bool = True,
    ) -> GuardVerdict:
        role = resolve_role(payload)
        import logging; logging.getLogger("moa.guard.service").warning("evaluate agent=%s intent=%s role=%s hitl=%s", agent_name, intent, role.value if role else None, hitl_enabled)
        logger.debug("guard evaluate agent=%s intent=%s role=%s", agent_name, intent, role.value)

        # 1. Deny: sensitive intents with insufficient role
        if intent in HITL_INTENTS and not has_permission(role, "execute", "agent"):
            return GuardVerdict(
                action=GuardianAction.DENY,
                reason=f"intent '{intent}' requires operator role, got {role.value}",
                role=role,
            )

        # 2. Deny: managing guard/prompt resources without admin
        resource = self._resolve_resource(intent, payload)
        if resource in SENSITIVE_RESOURCES and not has_permission(role, "manage", resource):
            return GuardVerdict(
                action=GuardianAction.DENY,
                reason=f"resource '{resource}' requires admin role, got {role.value}",
                role=role,
            )

        # 3. Review: HITL intents when hitl_enabled
        if intent in HITL_INTENTS and hitl_enabled:
            return GuardVerdict(
                action=GuardianAction.REVIEW,
                reason=f"intent '{intent}' requires human approval",
                role=role,
            )

        # 4. Allow: everything else
        return GuardVerdict(
            action=GuardianAction.ALLOW,
            reason="ok",
            role=role,
        )

    async def check(
        self,
        agent_name: str,
        slot: dict[str, Any],
        *,
        hitl_enabled: bool = True,
    ) -> GuardVerdict:
        """Async wrapper around evaluate for compatibility with existing callers."""
        intent = slot.get("intent", "assistant") if isinstance(slot, dict) else "assistant"
        return self.evaluate(agent_name, intent, slot, hitl_enabled=hitl_enabled)

    @staticmethod
    def _resolve_resource(intent: str, payload: dict[str, Any]) -> str:
        return payload.get("resource", intent) if isinstance(payload, dict) else intent

    def evaluate_output(
        self,
        text: str,
        *,
        intent: str = "assistant",
        role: Role | None = None,
        hitl_enabled: bool = True,
    ) -> tuple[GuardVerdict, tuple[str, ...]]:
        if role is None:
            role = resolve_role({"role": os.environ.get("MOA_DEFAULT_ROLE", "operator")})
        hits = policy_engine.check(text)
        policy_ids = tuple(hit.policy_id for hit in hits)
        if any(hit.severity == "deny" for hit in hits):
            return GuardVerdict(
                action=GuardianAction.DENY,
                reason=f"policy deny: {', '.join(policy_ids)}",
                role=role,
            ), policy_ids
        if any(hit.severity == "review" for hit in hits):
            return GuardVerdict(
                action=GuardianAction.REVIEW,
                reason=f"policy review: {', '.join(policy_ids)}",
                role=role,
            ), policy_ids
        return GuardVerdict(action=GuardianAction.ALLOW, reason="ok", role=role), ()


# Singleton for convenience.
guard_service = GuardService()
__all__ = ["GuardService", "GuardVerdict", "GuardianAction", "guard_service"]
