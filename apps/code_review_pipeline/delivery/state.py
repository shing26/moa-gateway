from __future__ import annotations

from apps.code_review_pipeline.delivery.models import DeliveryRunState

ALLOWED_TRANSITIONS: dict[DeliveryRunState, frozenset[DeliveryRunState]] = {
    DeliveryRunState.PREPARING: frozenset(
        {
            DeliveryRunState.IMPLEMENTING,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.IMPLEMENTING: frozenset(
        {
            DeliveryRunState.CHECKING,
            DeliveryRunState.BUDGET_EXHAUSTED,
            DeliveryRunState.FAILED,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.CHECKING: frozenset(
        {
            DeliveryRunState.VERIFIER_REVIEW,
            DeliveryRunState.READY_FOR_PUSH_APPROVAL,
            DeliveryRunState.REVISING,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.BUDGET_EXHAUSTED,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.VERIFIER_REVIEW: frozenset(
        {
            DeliveryRunState.READY_FOR_PUSH_APPROVAL,
            DeliveryRunState.REVISING,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.BUDGET_EXHAUSTED,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.REVISING: frozenset(
        {
            DeliveryRunState.CHECKING,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.BUDGET_EXHAUSTED,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.READY_FOR_PUSH_APPROVAL: frozenset(
        {
            DeliveryRunState.PUSHING,
            DeliveryRunState.REJECTED,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.PUSHING: frozenset(
        {
            DeliveryRunState.WAITING_CI,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.FAILED,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.WAITING_CI: frozenset(
        {
            DeliveryRunState.AWAITING_MERGE_APPROVAL,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.AWAITING_MERGE_APPROVAL: frozenset(
        {
            DeliveryRunState.MERGING,
            DeliveryRunState.REJECTED,
            DeliveryRunState.NEEDS_HUMAN,
            DeliveryRunState.ABANDONED,
        }
    ),
    DeliveryRunState.MERGING: frozenset(
        {DeliveryRunState.MERGED, DeliveryRunState.NEEDS_HUMAN}
    ),
    DeliveryRunState.MERGED: frozenset(),
    DeliveryRunState.REJECTED: frozenset(),
    DeliveryRunState.NEEDS_HUMAN: frozenset({DeliveryRunState.ABANDONED}),
    DeliveryRunState.BUDGET_EXHAUSTED: frozenset({DeliveryRunState.ABANDONED}),
    DeliveryRunState.FAILED: frozenset({DeliveryRunState.ABANDONED}),
    DeliveryRunState.ABANDONED: frozenset(),
}


class InvalidDeliveryTransition(Exception):
    def __init__(
        self,
        current: DeliveryRunState,
        target: DeliveryRunState,
    ) -> None:
        allowed = ", ".join(
            sorted(item.value for item in ALLOWED_TRANSITIONS[current])
        )
        super().__init__(
            f"delivery state {current.value!r} does not allow {target.value!r}"
            + (f"; allowed: {allowed}" if allowed else "; state is terminal")
        )


def assert_transition(
    current: DeliveryRunState,
    target: DeliveryRunState,
) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidDeliveryTransition(current, target)
