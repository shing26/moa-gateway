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
from apps.code_review_pipeline.merge_store import PostgresMergeStore


class _FakeCursor:
    def __init__(self, *, rowcount: int = 1) -> None:
        self.executed: list[tuple[str, object]] = []
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _FakePsycopg:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self, _dsn: str, **_kwargs) -> _FakeConn:
        return self._conn


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


class TestSupersedeOlder:
    """推新 commit 后旧 sha 的记录必须收尾（见 ``supersede_older``）。"""

    def test_older_open_records_are_superseded(self) -> None:
        store = MergeStore()
        store.save(_record(pr=7, sha="old"))
        store.save(_record(pr=7, sha="new"))

        superseded = store.supersede_older("o/r", 7, "new", at="t1")

        assert [r.head_sha for r in superseded] == ["old"]
        assert store.get(("o/r", 7, "old")).state == MergeState.SUPERSEDED.value  # type: ignore[union-attr]
        assert store.get(("o/r", 7, "new")).state == MergeState.WATCHING.value  # type: ignore[union-attr]
        # 旧记录不再被轮询，也就不会再为它弹卡片
        assert [r.head_sha for r in store.list_open()] == ["new"]

    def test_other_prs_are_untouched(self) -> None:
        store = MergeStore()
        store.save(_record(pr=7, sha="old"))
        store.save(_record(pr=8, sha="other"))

        store.supersede_older("o/r", 7, "new", at="t1")

        assert store.get(("o/r", 8, "other")).state == MergeState.WATCHING.value  # type: ignore[union-attr]

    def test_terminal_records_are_left_alone(self) -> None:
        """终态记录不该被改写：历史已经写下，取代动作不回溯修改它。"""
        store = MergeStore()
        store.save(_record(pr=7, sha="done"))
        identity = ("o/r", 7, "done")
        store.transition(identity, MergeAction.CI_GREEN, at="t")
        store.transition(identity, MergeAction.REJECT, at="t")

        assert store.supersede_older("o/r", 7, "new", at="t1") == []
        assert store.get(identity).state == MergeState.REJECTED.value  # type: ignore[union-attr]

    def test_awaiting_approval_can_be_superseded(self) -> None:
        """卡片已发出、人还没点，这时推了新 commit——那张卡片必须作废。"""
        store = MergeStore()
        store.save(_record(pr=7, sha="old"))
        store.transition(("o/r", 7, "old"), MergeAction.CI_GREEN, at="t")

        superseded = store.supersede_older("o/r", 7, "new", at="t1")

        assert [r.state for r in superseded] == [MergeState.SUPERSEDED.value]


class TestSetCardMessageId:
    def test_memory_write_back_is_visible_through_get(self) -> None:
        store = MergeStore()
        store.save(_record())

        store.set_card_message_id(("o/r", 7, "abc123"), "om_card_1")

        got = store.get(("o/r", 7, "abc123"))
        assert got is not None
        assert got.card_message_id == "om_card_1"

    def test_memory_missing_identity_raises(self) -> None:
        with pytest.raises(MergeNotFound):
            MergeStore().set_card_message_id(("o/r", 9, "nope"), "om_x")


def _postgres_store(
    *, rowcount: int = 1
) -> tuple[PostgresMergeStore, _FakeCursor, _FakeConn]:
    cursor = _FakeCursor(rowcount=rowcount)
    conn = _FakeConn(cursor)
    store = PostgresMergeStore(dsn="fake-dsn", _psycopg=_FakePsycopg(conn))
    return store, cursor, conn


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


class TestPostgresCardMessageId:
    def test_set_card_message_id_updates_only_that_column(self) -> None:
        store, cursor, conn = _postgres_store()

        store.set_card_message_id(("o/r", 7, "abc123"), "om_card_1")

        assert len(cursor.executed) == 1
        sql, params = cursor.executed[0]
        normalized = _normalized(sql)
        assert normalized == (
            "UPDATE pr_merge_gate SET card_message_id = %s "
            "WHERE repo = %s AND pr_number = %s AND head_sha = %s"
        )
        set_clause = normalized.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        assert set_clause == "card_message_id = %s"
        assert "updated_at" not in set_clause
        assert params == ("om_card_1", "o/r", 7, "abc123")
        assert conn.commits == 1

    def test_set_card_message_id_missing_record_raises_and_rolls_back(self) -> None:
        store, cursor, conn = _postgres_store(rowcount=0)

        with pytest.raises(MergeNotFound):
            store.set_card_message_id(("o/r", 7, "abc123"), "om_card_1")

        assert conn.rollbacks == 1
        assert conn.commits == 0

    def test_save_conflict_still_only_touches_updated_at(self) -> None:
        store, cursor, conn = _postgres_store()

        store.save(_record())

        assert len(cursor.executed) == 1
        sql, _params = cursor.executed[0]
        normalized = _normalized(sql)
        assert "ON CONFLICT (repo, pr_number, head_sha) DO UPDATE SET" in normalized
        conflict_clause = normalized.split("DO UPDATE SET ", 1)[1]
        assert conflict_clause == "updated_at = NOW()"
        assert "card_message_id" not in conflict_clause
        assert "state" not in conflict_clause
        assert conn.commits == 1
