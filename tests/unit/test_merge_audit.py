"""合并审批审计链的单测。

断言：
- 合并审批回调写入 ``human_decision`` 行
- operator 正确（谁批的）
- 成功/失败补 ``lifecycle`` 行
"""

from __future__ import annotations

import pytest

from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import MergeOutcome


def _make_record(
    repo: str = "owner/repo",
    pr_number: int = 42,
    head_sha: str = "abc123",
    state: str = MergeState.AWAITING_APPROVAL.value,
) -> MergeRecord:
    return MergeRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
    )


class _FakeGitHub:
    def __init__(self, merge_outcome: MergeOutcome | None = None) -> None:
        self._merge_outcome = merge_outcome or MergeOutcome(
            merged=True, status="merged", sha="def456"
        )

    async def get_pr(self, repo, pr_number: int) -> dict:
        return {"mergeable": True, "mergeable_state": "clean"}

    async def merge_pr(self, repo, pr_number: int, *, sha: str = "") -> MergeOutcome:
        return self._merge_outcome


@pytest.mark.asyncio
async def test_merge_approval_writes_human_decision_audit(monkeypatch) -> None:
    """合并审批回调必须写入 ``human_decision`` 审计行。"""
    from app.routes import webhook

    record = _make_record()
    store = MergeStore()
    store.save(record)

    # Mock merge_store 和 build_github_client
    monkeypatch.setattr(webhook, "merge_store", store)

    fake_gh = _FakeGitHub()
    monkeypatch.setattr(webhook, "build_github_client", lambda: fake_gh)

    # Mock record_human_decision 捕获调用
    audit_calls: list[dict] = []

    async def _mock_record_human_decision(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_human_decision",
        _mock_record_human_decision,
    )

    response = await webhook._handle_merge_approval(
        hitl_id=record.task_key,
        action="approve",
        operator_id="user_123",
        session_id=record.task_key,
        trace_id=record.task_key,
    )

    assert response.status_code == 200
    assert len(audit_calls) == 1
    assert audit_calls[0]["task_id"] == record.task_key
    assert audit_calls[0]["operator"] == "user_123"
    assert audit_calls[0]["decision"] == "approve"


@pytest.mark.asyncio
async def test_merge_rejection_writes_human_decision_audit(monkeypatch) -> None:
    """合并拒绝回调也必须写入 ``human_decision`` 审计行。"""
    from app.routes import webhook

    record = _make_record()
    store = MergeStore()
    store.save(record)

    monkeypatch.setattr(webhook, "merge_store", store)
    monkeypatch.setattr(webhook, "build_github_client", lambda: _FakeGitHub())

    audit_calls: list[dict] = []

    async def _mock_record_human_decision(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_human_decision",
        _mock_record_human_decision,
    )

    response = await webhook._handle_merge_approval(
        hitl_id=record.task_key,
        action="reject",
        operator_id="user_456",
        session_id=record.task_key,
        trace_id=record.task_key,
    )

    assert response.status_code == 200
    assert len(audit_calls) == 1
    assert audit_calls[0]["operator"] == "user_456"
    assert audit_calls[0]["decision"] == "reject"
