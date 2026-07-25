# ADR-002: RBAC Guard + HITL 审批

**Status**: Accepted
**Date**: 2026-07-24
**Supersedes**: ADR-001 (Fail-Closed Guard stubs)

## Context

Stage 1 implemented a basic FailClosedPermissionGuard stub that only checked
schema validity. v0.5 requires a proper authorization layer with:
- Role-based access control (admin/operator/viewer)
- Three-level guard routing (ALLOW/REVIEW/DENY)
- Human-in-the-loop approval via Feishu interactive cards

## Decision

1. RBAC model with three roles: VIEWER, OPERATOR, ADMIN
2. GuardService with three actions: ALLOW (direct pass), REVIEW (trigger HITL card),
   DENY (block with reason)
3. Feishu approval cards for HITL flow, with /webhook/callback endpoint
4. FailClosedPermissionGuard kept as legacy safety layer but deprecated

### Implementation
- app/guard/rbac.py: Role enum, Permission dataclass, resolve_role()
- app/guard/guard_service.py: GuardService with evaluate()
- app/channels/feishu_cards.py: ApprovalCard, FeishuCardSender
- app/engine.py: HitlRequest storage, NEEDS_HUMAN state transition

## Consequences

Positive: Clear auth seam, testable in isolation, HITL cards work without
blocking the main request path.
Negative: Two guard implementations co-exist temporarily (GuardService +
FailClosedPermissionGuard). Need to consolidate in next phase.
