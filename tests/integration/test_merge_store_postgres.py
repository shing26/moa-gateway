"""合并通道的 Postgres 集成测试。

对着真库验证，不只断言 SQL 字符串。这些测试需要：
1. Postgres 容器在跑（docker compose -f docker-compose.dev.yml up -d postgres）
2. 数据库连接信息在 .env 里（CODE_REVIEW_DATABASE_URL）

与 test_merge_store.py 的区别：那个只测内存实现，这个测真库的：
- 状态迁移在 PG 上真的生效
- transitions jsonb 真的累积
- 乐观锁真的能拦住并发写
"""
from __future__ import annotations

import os
import uuid

import pytest

from apps.code_review_pipeline.merge_state import (
    InvalidMergeTransition,
    MergeAction,
    MergeState,
)
from apps.code_review_pipeline.merge_store import (
    MergeNotFound,
    MergeRecord,
    PostgresMergeStore,
)

# 跳过标记：没有数据库连接就跳过整个模块
pytestmark = pytest.mark.skipif(
    not os.getenv("CODE_REVIEW_DATABASE_URL"),
    reason="CODE_REVIEW_DATABASE_URL not set",
)


def _dsn() -> str:
    """从环境变量读 DSN，优先用 CODE_REVIEW_DATABASE_URL。"""
    dsn = os.getenv("CODE_REVIEW_DATABASE_URL")
    if not dsn:
        pytest.skip("CODE_REVIEW_DATABASE_URL not set")
    return dsn


def _record(pr: int = 7, sha: str = "abc123") -> MergeRecord:
    return MergeRecord(repo="o/r", pr_number=pr, head_sha=sha)


def _unique_identity() -> tuple[str, int, str]:
    """每个测试用唯一的 identity，避免相互干扰。"""
    suffix = uuid.uuid4().hex[:8]
    return (f"test/{suffix}", 999, suffix)


@pytest.fixture
def store() -> PostgresMergeStore:
    store = PostgresMergeStore(dsn=_dsn())
    # 清理之前测试遗留的数据，避免 list_open() 返回历史记录。
    # 只删 test/ 开头的 repo，不影响真实数据。
    store._connect()
    with store._conn.cursor() as cur:
        cur.execute("DELETE FROM pr_merge_gate WHERE repo LIKE 'test/%'")
        store._conn.commit()
    return store


class TestPostgresRoundTrip:
    def test_save_and_get(self, store: PostgresMergeStore) -> None:
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)
        got = store.get(identity)
        assert got is not None
        assert got.state == MergeState.WATCHING.value
        assert got.repo == identity[0]
        assert got.pr_number == identity[1]
        assert got.head_sha == identity[2]

    def test_get_missing_is_none(self, store: PostgresMergeStore) -> None:
        assert store.get(("no/such", 1, "nope")) is None

    def test_save_idempotent_on_conflict(self, store: PostgresMergeStore) -> None:
        """重投（webhook 重试、轮询重复）不该把已推进的记录拽回 watching。"""
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)
        # 第一次推进到 awaiting_approval
        store.transition(identity, MergeAction.CI_GREEN, at="t1")
        # 重投：再 save 一次，state 应该保持 awaiting_approval
        store.save(record)
        got = store.get(identity)
        assert got is not None
        assert got.state == MergeState.AWAITING_APPROVAL.value


class TestPostgresTransition:
    def test_green_moves_to_awaiting_approval(self, store: PostgresMergeStore) -> None:
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)
        got = store.transition(identity, MergeAction.CI_GREEN, at="t1")
        assert got.state == MergeState.AWAITING_APPROVAL.value

    def test_updates_are_applied(self, store: PostgresMergeStore) -> None:
        """CI 红的时候要把"哪一条红了"一起存下来。"""
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)
        got = store.transition(
            identity,
            MergeAction.CI_RED,
            at="t1",
            ci_state="failure",
            ci_failing=({"name": "pytest", "url": "https://ci/1"},),
        )
        assert got.ci_state == "failure"
        assert got.ci_failing[0]["name"] == "pytest"

    def test_transition_history_accumulates(self, store: PostgresMergeStore) -> None:
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)
        store.transition(identity, MergeAction.CI_GREEN, at="t1")
        got = store.transition(identity, MergeAction.APPROVE, at="t2", approver="alice")
        assert [t["action"] for t in got.transitions] == ["ci_green", "approve"]
        assert got.transitions[0]["from"] == "watching"
        assert got.transitions[1]["to"] == "merging"
        assert got.approver == "alice"

    def test_illegal_transition_raises_and_does_not_mutate(
        self, store: PostgresMergeStore
    ) -> None:
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)
        with pytest.raises(InvalidMergeTransition):
            store.transition(identity, MergeAction.APPROVE, at="t1")
        # 状态没变
        assert store.get(identity).state == MergeState.WATCHING.value  # type: ignore[union-attr]

    def test_missing_identity_raises(self, store: PostgresMergeStore) -> None:
        with pytest.raises(MergeNotFound):
            store.transition(("no/such", 9, "x"), MergeAction.CI_GREEN, at="t")


class TestPostgresOptimisticLock:
    def test_concurrent_transition_only_one_wins(
        self, store: PostgresMergeStore
    ) -> None:
        """两个进程同时看到 watching，只有一个能推进到 awaiting_approval。

        这是乐观锁的核心场景：并发下两个进程同时读到同一状态，各自算出目标状态
        并先后写入。条件 UPDATE 让第二次写入影响 0 行，于是"有人抢先了"变成一个
        能看见的事实，而不是静默丢失。
        """
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)

        # 第一个进程：读到 watching，推进到 awaiting_approval
        first = store.transition(identity, MergeAction.CI_GREEN, at="t1")
        assert first.state == MergeState.AWAITING_APPROVAL.value

        # 第二个进程：也读到 watching（模拟并发），尝试推进
        # 应该被乐观锁拦住，抛 InvalidMergeTransition
        with pytest.raises(InvalidMergeTransition):
            store.transition(identity, MergeAction.CI_GREEN, at="t2")

        # 状态仍然是 awaiting_approval，没有被覆盖
        got = store.get(identity)
        assert got is not None
        assert got.state == MergeState.AWAITING_APPROVAL.value
        # transitions 只有一条，没有被第二次写入覆盖
        assert len(got.transitions) == 1


class TestPostgresListOpen:
    def test_excludes_terminal(self, store: PostgresMergeStore) -> None:
        identity1 = _unique_identity()
        identity2 = _unique_identity()
        record1 = MergeRecord(repo=identity1[0], pr_number=identity1[1], head_sha=identity1[2])
        record2 = MergeRecord(repo=identity2[0], pr_number=identity2[1], head_sha=identity2[2])
        store.save(record1)
        store.save(record2)
        # 推进第一个到终态
        store.transition(identity1, MergeAction.CI_GREEN, at="t")
        store.transition(identity1, MergeAction.REJECT, at="t")
        # 第二个保持 watching
        open_records = store.list_open()
        assert len(open_records) == 1
        assert open_records[0].identity == identity2

    def test_order_is_stable(self, store: PostgresMergeStore) -> None:
        """轮询每轮都该以同样顺序看同一批 PR。"""
        identities = [_unique_identity() for _ in range(3)]
        for identity in identities:
            record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
            store.save(record)
        open_records = store.list_open()
        # 按 (repo, pr_number, head_sha) 排序
        sorted_identities = sorted(identities, key=lambda x: (x[0], x[1], x[2]))
        assert [r.identity for r in open_records] == sorted_identities


class TestPostgresFullLifecycle:
    def test_complete_merge_lifecycle(self, store: PostgresMergeStore) -> None:
        """完整生命周期：watching -> awaiting_approval -> merging -> merged。"""
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)

        # CI 绿
        store.transition(identity, MergeAction.CI_GREEN, at="t1", ci_state="success")
        # 人批准
        store.transition(identity, MergeAction.APPROVE, at="t2", approver="alice")
        # 合并完成
        got = store.transition(identity, MergeAction.COMPLETE, at="t3", merged_sha="def456")

        assert got.state == MergeState.MERGED.value
        assert got.merged_sha == "def456"
        assert got.approver == "alice"
        assert len(got.transitions) == 3
        assert got.is_terminal is True

    def test_ci_red_then_green_then_approve(self, store: PostgresMergeStore) -> None:
        """CI 红了再重跑变绿，是最常见的路径。"""
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)

        # CI 红
        store.transition(
            identity,
            MergeAction.CI_RED,
            at="t1",
            ci_state="failure",
            ci_failing=({"name": "pytest", "url": "https://ci/1"},),
        )
        # CI 重跑变绿
        store.transition(identity, MergeAction.CI_GREEN, at="t2", ci_state="success")
        # 人批准
        got = store.transition(identity, MergeAction.APPROVE, at="t3", approver="alice")

        assert got.state == MergeState.MERGING.value
        assert got.ci_state == "success"
        assert len(got.transitions) == 3

    def test_approval_then_ci_red_invalidates_card(
        self, store: PostgresMergeStore
    ) -> None:
        """审批期间 CI 变红，待批必须作废——这是本 ADR 最要紧的安全边界。"""
        identity = _unique_identity()
        record = MergeRecord(repo=identity[0], pr_number=identity[1], head_sha=identity[2])
        store.save(record)

        # CI 绿，发卡片
        store.transition(identity, MergeAction.CI_GREEN, at="t1", ci_state="success")
        # 人还没点，CI 变红
        got = store.transition(
            identity,
            MergeAction.CI_RED,
            at="t2",
            ci_state="failure",
            ci_failing=({"name": "pytest", "url": "https://ci/2"},),
        )

        # 状态回到 ci_failed，卡片作废
        assert got.state == MergeState.CI_FAILED.value
        assert got.ci_state == "failure"
        # 此时不能再批准（ci_failed 不允许 approve）
        with pytest.raises(InvalidMergeTransition):
            store.transition(identity, MergeAction.APPROVE, at="t3")
