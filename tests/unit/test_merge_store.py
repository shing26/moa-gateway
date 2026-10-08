"""合并通道的存储（内存实现）。
状态机的逻辑已在 test_merge_state 里钉过，这里只验"存进去、取出来、迁移留痕"。
"""
from __future__ import annotations

import pytest

from apps.code_review_pipeline.merge_state import (
    InvalidMergeTransition,
    MergeAction,
    MergeState,
)
from apps.code_review_pipeline.merge_store import MergeNotFound, MergeRecord, MergeStore


def _record(pr: int = 7, sha: str = "abc123") -> MergeRecord:
    return MergeRecord(repo="o/r", pr_number=pr, head_sha=sha)


class TestRoundTrip:
    def test_save_and_get(self) -> None:
        store = MergeStore()
        store.save(_record())
        got = store.get(("o/r", 7, "abc123"))
        assert got is not None
        assert got.state == MergeState.WATCHING.value

    def test_get_missing_is_none(self) -> None:
        assert MergeStore().get(("o/r", 7, "nope")) is None

    def test_identity_and_task_key(self) -> None:
        rec = _record()
        assert rec.identity == ("o/r", 7, "abc123")
        assert rec.task_key == "o/r#7@abc123"


class TestTransition:
    def test_green_moves_to_awaiting_approval(self) -> None:
        store = MergeStore()
        store.save(_record())
        got = store.transition(("o/r", 7, "abc123"), MergeAction.CI_GREEN, at="t1")
        assert got.state == MergeState.AWAITING_APPROVAL.value

    def test_updates_are_applied(self) -> None:
        """CI 红的时候要把"哪一条红了"一起存下来，卡片和日志都靠它。"""
        store = MergeStore()
        store.save(_record())
        got = store.transition(
            ("o/r", 7, "abc123"),
            MergeAction.CI_RED,
            at="t1",
            ci_state="failure",
            ci_failing=({"name": "pytest", "url": "https://ci/1"},),
        )
        assert got.ci_state == "failure"
        assert got.ci_failing[0]["name"] == "pytest"

    def test_transition_history_accumulates(self) -> None:
        store = MergeStore()
        store.save(_record())
        identity = ("o/r", 7, "abc123")
        store.transition(identity, MergeAction.CI_GREEN, at="t1")
        got = store.transition(identity, MergeAction.APPROVE, at="t2", approver="alice")
        assert [t["action"] for t in got.transitions] == ["ci_green", "approve"]
        assert got.transitions[0]["from"] == "watching"
        assert got.transitions[1]["to"] == "merging"
        assert got.approver == "alice"

    def test_illegal_transition_raises_and_does_not_mutate(self) -> None:
        store = MergeStore()
        store.save(_record())
        identity = ("o/r", 7, "abc123")
        with pytest.raises(InvalidMergeTransition):
            store.transition(identity, MergeAction.APPROVE, at="t1")
        assert store.get(identity).state == MergeState.WATCHING.value  # type: ignore[union-attr]

    def test_missing_identity_raises(self) -> None:
        with pytest.raises(MergeNotFound):
            MergeStore().transition(("o/r", 9, "x"), MergeAction.CI_GREEN, at="t")


class TestListOpen:
    def test_excludes_terminal(self) -> None:
        store = MergeStore()
        store.save(_record(pr=1, sha="s1"))
        store.save(_record(pr=2, sha="s2"))
        identity = ("o/r", 1, "s1")
        store.transition(identity, MergeAction.CI_GREEN, at="t")
        store.transition(identity, MergeAction.REJECT, at="t")
        open_records = store.list_open()
        assert [r.pr_number for r in open_records] == [2]

    def test_order_is_stable(self) -> None:
        """轮询每轮都该以同样顺序看同一批 PR，否则日志行序无法比对。"""
        store = MergeStore()
        store.save(_record(pr=9, sha="s9"))
        store.save(_record(pr=2, sha="s2"))
        assert [r.pr_number for r in store.list_open()] == [2, 9]

    def test_terminal_flag(self) -> None:
        store = MergeStore()
        store.save(_record())
        identity = ("o/r", 7, "abc123")
        assert store.get(identity).is_terminal is False  # type: ignore[union-attr]
        store.transition(identity, MergeAction.CI_GREEN, at="t")
        store.transition(identity, MergeAction.REJECT, at="t")
        assert store.get(identity).is_terminal is True  # type: ignore[union-attr]
