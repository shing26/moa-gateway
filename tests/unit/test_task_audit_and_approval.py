"""任务审计三类行 + 审批流（D4）。

判据（计划里的原话）：``trace_id`` 用 task_id，一个任务在审计里能捞出
5 条 agent + 1 条任务 lifecycle + 1 条人工决策。
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.code_review_pipeline.approval import approve_task, reject_task
from apps.code_review_pipeline.task_audit import (
    AGENT_NAMES,
    HUMAN_DECISION_AGENT,
    LIFECYCLE_AGENT,
    record_agent_rows,
    record_human_decision,
    record_lifecycle,
)

TASK_ID = "cr_o/r#42@abc1234"
REPO = "o/r"
IDENTITY = ("o/r", 42, "abc1234")


class _Collector:
    """顶替 app.audit.recorder.record。"""

    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def __call__(self, entry: Any) -> None:
        self.entries.append(entry)


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> _Collector:
    c = _Collector()
    monkeypatch.setattr("apps.code_review_pipeline.task_audit.record", c)
    return c


class _Finding:
    def __init__(self, severity: str) -> None:
        self.severity = severity


class _Section:
    def __init__(self, findings: list[_Finding] | None = None) -> None:
        self.findings = findings or []


class _Result:
    def __init__(self) -> None:
        self.triage = _Section([_Finding("high"), _Finding("low")])
        self.static_analysis = _Section()
        self.semantic_review = _Section([_Finding("high")])
        self.test_coverage = _Section()
        self.report = _Section([_Finding("info")])


@pytest.mark.asyncio
async def test_five_agent_rows_are_written_with_task_id_as_trace(
    collector: _Collector,
) -> None:
    written = await record_agent_rows(TASK_ID, REPO, _Result())
    assert written == 5
    assert [e.agent_name for e in collector.entries] == list(AGENT_NAMES)
    assert {e.trace_id for e in collector.entries} == {TASK_ID}
    assert {e.session_id for e in collector.entries} == {REPO}


@pytest.mark.asyncio
async def test_agent_rows_record_finding_counts(collector: _Collector) -> None:
    await record_agent_rows(TASK_ID, REPO, _Result())
    counts = {e.agent_name: e.extra.get("findings") for e in collector.entries}
    assert counts["triage"] == 2
    assert counts["static_analysis"] == 0
    assert counts["semantic_review"] == 1


@pytest.mark.asyncio
async def test_agent_row_output_is_a_summary_not_the_full_findings(
    collector: _Collector,
) -> None:
    """审计不该替代结果存储：一行几千字会撑爆 jsonl。"""
    await record_agent_rows(TASK_ID, REPO, _Result())
    triage = next(e for e in collector.entries if e.agent_name == "triage")
    assert "high=1" in triage.agent_output
    assert len(triage.agent_output) < 200


@pytest.mark.asyncio
async def test_lifecycle_and_human_rows_are_distinguishable(collector: _Collector) -> None:
    await record_lifecycle(TASK_ID, REPO, "posting", operator="alice")
    await record_human_decision(TASK_ID, REPO, operator="alice", decision="approve")
    kinds = [e.agent_name for e in collector.entries]
    assert kinds == [LIFECYCLE_AGENT, HUMAN_DECISION_AGENT]
    assert LIFECYCLE_AGENT not in AGENT_NAMES
    assert HUMAN_DECISION_AGENT not in AGENT_NAMES


@pytest.mark.asyncio
async def test_human_decision_records_who(collector: _Collector) -> None:
    """"谁按的按钮"必须落库——否则审批单据少一个基本字段。"""
    await record_human_decision(TASK_ID, REPO, operator="alice", decision="approve")
    entry = collector.entries[0]
    assert entry.extra["operator"] == "alice"
    assert entry.extra["decision"] == "approve"


class FakeStore:
    def __init__(self) -> None:
        self.posted_review_id: str | None = None
        self.transitions: list[str] = []

    def get_posted_review_id(self, identity: tuple[str, int, str]) -> str | None:
        return self.posted_review_id

    def set_posted_review_id(self, identity: tuple[str, int, str], review_id: str) -> None:
        self.posted_review_id = review_id

    def transition_task(self, identity: tuple[str, int, str], action: str) -> str:
        self.transitions.append(action)
        return action


class FakeGitHub:
    def __init__(self) -> None:
        self.posted: list[str] = []

    async def list_reviews(self, repo: Any, pr_number: int) -> list[dict[str, Any]]:
        return []

    async def create_review(
        self, repo: Any, pr_number: int, body: str, *, event: str = "COMMENT"
    ) -> dict[str, Any]:
        self.posted.append(body)
        return {"id": 4242}


@pytest.mark.asyncio
async def test_approve_posts_records_id_and_completes(
    collector: _Collector,
) -> None:
    store = FakeStore()
    gh = FakeGitHub()
    outcome = await approve_task(store, gh, IDENTITY, TASK_ID, operator="alice")

    assert outcome.status == "posted"
    assert store.posted_review_id == "4242"
    assert store.transitions == ["approve", "complete"]
    assert len(gh.posted) == 1


@pytest.mark.asyncio
async def test_approve_writes_audit_before_posting(collector: _Collector) -> None:
    """审计先于写回：崩在中间不能留下"已批准但无记录"。"""
    store = FakeStore()
    gh = FakeGitHub()
    await approve_task(store, gh, IDENTITY, TASK_ID, operator="alice")
    kinds = [e.agent_name for e in collector.entries]
    assert kinds[0] == HUMAN_DECISION_AGENT
    assert LIFECYCLE_AGENT in kinds


@pytest.mark.asyncio
async def test_dry_run_approve_has_no_side_effects(
    collector: _Collector,
) -> None:
    """dry-run **完全不动状态**：不发评论、不记 id、不推进、不写审计。

    回归（2026-10-02 golden path）：原实现先 ``approve -> posting`` 再跑 dry-run，
    把任务留在 posting。而 posting 只允许 complete/fail，于是"先试跑、再真跑"这条
    最自然的路径永久卡死——``demo approve`` 之后接 ``demo approve --real`` 直接抛
    InvalidTaskTransition。dry-run 的意义就是预演，预演推进过的状态没法重推。
    """
    store = FakeStore()
    gh = FakeGitHub()
    outcome = await approve_task(
        store, gh, IDENTITY, TASK_ID, operator="alice", dry_run=True
    )
    assert outcome.status == "dry_run"
    assert gh.posted == []
    assert store.posted_review_id is None, "dry-run 记了 id 会让真跑被主判据挡掉"
    assert store.transitions == [], "dry-run 推进了状态，真跑就再也推进不了"
    assert collector.entries == [], "dry-run 写了审计，等于宣称发生过一次决策"


@pytest.mark.asyncio
async def test_dry_run_then_real_approve_succeeds(collector: _Collector) -> None:
    """先试跑再真跑必须走得通——这是 dry-run 存在的意义。"""
    store = FakeStore()
    gh = FakeGitHub()
    await approve_task(store, gh, IDENTITY, TASK_ID, operator="alice", dry_run=True)
    outcome = await approve_task(store, gh, IDENTITY, TASK_ID, operator="alice")
    assert outcome.status == "posted"
    assert store.transitions == ["approve", "complete"]


@pytest.mark.asyncio
async def test_approve_twice_is_idempotent_not_a_crash(collector: _Collector) -> None:
    """重复批准应报"已写回"，而不是抛 InvalidTaskTransition。

    已写回的任务处于终态 done，而 done 不允许任何动作。所以这个短路必须发生在
    状态迁移**之前**——否则人看到的是一坨状态机栈，怎么也想不到"我只是又点了
    一次批准"。
    """
    store = FakeStore()
    gh = FakeGitHub()
    await approve_task(store, gh, IDENTITY, TASK_ID, operator="alice")
    again = await approve_task(store, gh, IDENTITY, TASK_ID, operator="alice")
    assert again.status == "already_posted"
    assert again.review_id == "4242"
    assert len(gh.posted) == 1, "第二次批准又发了一条评论"

@pytest.mark.asyncio
async def test_reject_never_posts(collector: _Collector) -> None:
    """拒绝后不写任何评论：人已经否掉了，再留言只是噪声。"""
    store = FakeStore()
    gh = FakeGitHub()
    assert await reject_task(store, IDENTITY, TASK_ID, operator="bob") == "rejected"
    assert gh.posted == []
    assert store.posted_review_id is None
    assert store.transitions == ["reject"]
