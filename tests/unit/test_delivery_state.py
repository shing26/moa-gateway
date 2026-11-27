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
        (DeliveryRunState.PUSHING, DeliveryRunState.NEEDS_HUMAN),
        (DeliveryRunState.WAITING_CI, DeliveryRunState.AWAITING_MERGE_APPROVAL),
        (DeliveryRunState.AWAITING_MERGE_APPROVAL, DeliveryRunState.MERGING),
        (DeliveryRunState.MERGING, DeliveryRunState.MERGED),
        (DeliveryRunState.WAITING_CI, DeliveryRunState.ABANDONED),
        (DeliveryRunState.NEEDS_HUMAN, DeliveryRunState.ABANDONED),
    ],
)
def test_legal_transitions(
    current: DeliveryRunState, target: DeliveryRunState
) -> None:
    assert_transition(current, target)
    assert target in ALLOWED_TRANSITIONS[current]


def test_terminal_states_allow_only_abandon() -> None:
    for state in (
        DeliveryRunState.MERGED,
        DeliveryRunState.REJECTED,
        DeliveryRunState.NEEDS_HUMAN,
        DeliveryRunState.BUDGET_EXHAUSTED,
        DeliveryRunState.FAILED,
    ):
        expected = (
            frozenset()
            if state in {DeliveryRunState.MERGED, DeliveryRunState.REJECTED}
            else frozenset({DeliveryRunState.ABANDONED})
        )
        assert ALLOWED_TRANSITIONS[state] == expected
    assert ALLOWED_TRANSITIONS[DeliveryRunState.ABANDONED] == frozenset()


@pytest.mark.parametrize(
    "state",
    [
        DeliveryRunState.PREPARING,
        DeliveryRunState.IMPLEMENTING,
        DeliveryRunState.CHECKING,
        DeliveryRunState.VERIFIER_REVIEW,
        DeliveryRunState.REVISING,
        DeliveryRunState.READY_FOR_PUSH_APPROVAL,
        DeliveryRunState.PUSHING,
        DeliveryRunState.WAITING_CI,
        DeliveryRunState.AWAITING_MERGE_APPROVAL,
        DeliveryRunState.NEEDS_HUMAN,
        DeliveryRunState.FAILED,
        DeliveryRunState.BUDGET_EXHAUSTED,
    ],
)
def test_abandon_is_legal_from_contract_states(
    state: DeliveryRunState,
) -> None:
    assert_transition(state, DeliveryRunState.ABANDONED)


@pytest.mark.parametrize(
    "state",
    [
        DeliveryRunState.MERGING,
        DeliveryRunState.MERGED,
        DeliveryRunState.REJECTED,
        DeliveryRunState.ABANDONED,
    ],
)
def test_abandon_is_refused_from_closed_states(
    state: DeliveryRunState,
) -> None:
    with pytest.raises(InvalidDeliveryTransition):
        assert_transition(state, DeliveryRunState.ABANDONED)


def test_illegal_transition_names_both_states() -> None:
    with pytest.raises(InvalidDeliveryTransition) as caught:
        assert_transition(
            DeliveryRunState.PREPARING,
            DeliveryRunState.PUSHING,
        )
    assert "preparing" in str(caught.value)
    assert "pushing" in str(caught.value)
