from __future__ import annotations

import pytest

from apps.code_review_pipeline.delivery.models import DeliveryRunState
from apps.code_review_pipeline.delivery.state import (
    ALLOWED_TRANSITIONS,
    InvalidDeliveryTransition,
    assert_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeliveryRunState.PREPARING, DeliveryRunState.IMPLEMENTING),
        (DeliveryRunState.IMPLEMENTING, DeliveryRunState.CHECKING),
        (DeliveryRunState.CHECKING, DeliveryRunState.READY_FOR_PUSH_APPROVAL),
        (DeliveryRunState.CHECKING, DeliveryRunState.REVISING),
        (DeliveryRunState.VERIFIER_REVIEW, DeliveryRunState.BUDGET_EXHAUSTED),
        (DeliveryRunState.REVISING, DeliveryRunState.BUDGET_EXHAUSTED),
        (DeliveryRunState.READY_FOR_PUSH_APPROVAL, DeliveryRunState.PUSHING),
        (DeliveryRunState.PUSHING, DeliveryRunState.WAITING_CI),
        (DeliveryRunState.WAITING_CI, DeliveryRunState.AWAITING_MERGE_APPROVAL),
        (DeliveryRunState.AWAITING_MERGE_APPROVAL, DeliveryRunState.MERGING),
        (DeliveryRunState.MERGING, DeliveryRunState.MERGED),
    ],
)
def test_legal_transitions(
    current: DeliveryRunState, target: DeliveryRunState
) -> None:
    assert_transition(current, target)
    assert target in ALLOWED_TRANSITIONS[current]


def test_terminal_states_do_not_transition() -> None:
    for state in (
        DeliveryRunState.MERGED,
        DeliveryRunState.REJECTED,
        DeliveryRunState.NEEDS_HUMAN,
        DeliveryRunState.BUDGET_EXHAUSTED,
        DeliveryRunState.FAILED,
    ):
        assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_illegal_transition_names_both_states() -> None:
    with pytest.raises(InvalidDeliveryTransition) as caught:
        assert_transition(
            DeliveryRunState.PREPARING,
            DeliveryRunState.PUSHING,
        )
    assert "preparing" in str(caught.value)
    assert "pushing" in str(caught.value)
