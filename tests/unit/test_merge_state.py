"""合并通道状态机（ADR-0021）。
重点不在"正常路径能走通"，而在三条边界：
1. 审批期间 CI 变红必须能作废卡片，否则人会批准一个 CI 正红的版本。
2. CI 红不是终态，CI 重跑变绿是最常见的路径。
3. 终态不可回退，重复投递在这一层就该被挡住。
"""
from __future__ import annotations

import pytest

from apps.code_review_pipeline.merge_state import (
    InvalidMergeTransition,
    MergeAction,
    MergeState,
    can,
    is_terminal,
    next_state,
    should_notify_approval,
)


class TestHappyPath:
    def test_watching_to_merged(self) -> None:
        state = MergeState.WATCHING
        state = next_state(state, MergeAction.CI_GREEN)
        assert state is MergeState.AWAITING_APPROVAL
        state = next_state(state, MergeAction.APPROVE)
        assert state is MergeState.MERGING
        state = next_state(state, MergeAction.COMPLETE)
        assert state is MergeState.MERGED

    def test_reject_is_its_own_terminal(self) -> None:
        state = next_state(MergeState.WATCHING, MergeAction.CI_GREEN)
        state = next_state(state, MergeAction.REJECT)
        assert state is MergeState.REJECTED
        assert is_terminal(state)


class TestCiRedIsRecoverable:
    def test_ci_red_is_not_terminal(self) -> None:
        state = next_state(MergeState.WATCHING, MergeAction.CI_RED)
        assert state is MergeState.CI_FAILED
        assert is_terminal(state) is False

    def test_ci_red_then_green_after_rerun(self) -> None:
        """CI 重跑变绿是最常见的路径。一次红就锁死会让通道没法用。"""
        state = next_state(MergeState.WATCHING, MergeAction.CI_RED)
        state = next_state(state, MergeAction.CI_GREEN)
        assert state is MergeState.AWAITING_APPROVAL

    def test_repeated_red_is_idempotent(self) -> None:
        """轮询会反复看到同一个红结论，不该因此抛异常。"""
        state = next_state(MergeState.WATCHING, MergeAction.CI_RED)
        state = next_state(state, MergeAction.CI_RED)
        assert state is MergeState.CI_FAILED


class TestCardInvalidation:
    """本 ADR 最要紧的一条安全边界。"""

    def test_ci_red_while_awaiting_approval_is_allowed(self) -> None:
        """卡片发出后人还没点，CI 变红——卡片描述的是已不成立的结论，必须能作废。"""
        assert can(MergeState.AWAITING_APPROVAL, MergeAction.CI_RED)

    def test_ci_red_invalidates_pending_card(self) -> None:
        state = next_state(MergeState.AWAITING_APPROVAL, MergeAction.CI_RED)
        assert state is MergeState.CI_FAILED

    def test_invalidated_card_does_not_notify_a_new_approval(self) -> None:
        assert should_notify_approval(MergeState.AWAITING_APPROVAL, MergeAction.CI_RED) is False

    def test_regreen_after_invalidation_notifies_again(self) -> None:
        """作废之后 CI 又绿了，应该重新发一张卡片。"""
        assert should_notify_approval(MergeState.CI_FAILED, MergeAction.CI_GREEN) is True


class TestNotifySemantics:
    def test_first_green_notifies(self) -> None:
        assert should_notify_approval(MergeState.WATCHING, MergeAction.CI_GREEN) is True

    def test_approve_does_not_notify(self) -> None:
        assert should_notify_approval(MergeState.AWAITING_APPROVAL, MergeAction.APPROVE) is False

    def test_terminal_state_notifies_nothing(self) -> None:
        assert should_notify_approval(MergeState.MERGED, MergeAction.CI_GREEN) is False


class TestTerminalsAreFinal:
    @pytest.mark.parametrize(
        "terminal",
        [MergeState.MERGED, MergeState.REJECTED, MergeState.FAILED],
    )
    @pytest.mark.parametrize("action", list(MergeAction))
    def test_no_action_is_allowed(self, terminal: MergeState, action: MergeAction) -> None:
        assert can(terminal, action) is False
        with pytest.raises(InvalidMergeTransition):
            next_state(terminal, action)

    def test_merging_does_not_accept_approval_twice(self) -> None:
        """合并中不接受第二次批准——动作已经交出去了，此刻由 GitHub 决定。"""
        assert can(MergeState.MERGING, MergeAction.APPROVE) is False


class TestErrorIsReadable:
    def test_error_names_the_allowed_actions(self) -> None:
        with pytest.raises(InvalidMergeTransition) as exc:
            next_state(MergeState.WATCHING, MergeAction.APPROVE)
        assert "ci_green" in str(exc.value)
        assert "ci_red" in str(exc.value)

    def test_error_says_terminal_when_there_is_nothing_allowed(self) -> None:
        with pytest.raises(InvalidMergeTransition) as exc:
            next_state(MergeState.MERGED, MergeAction.APPROVE)
        assert "terminal" in str(exc.value)
