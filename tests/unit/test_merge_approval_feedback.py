"""合并审批回调的**用户反馈**（2026-10-09 修复）。

此前 ``_handle_merge_approval`` 收下卡片回调里的 chat_id 却从不发消息，成功路径
只返回一个飞书不认识的 JSON——人点了"批准"，合并确实执行了，但界面上什么也不会
发生。这三个用例钉住"结果必须回给发起人"。
"""

from __future__ import annotations

import pytest

from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.routing.github_client import MergeOutcome


def _make_record(state: str = MergeState.AWAITING_APPROVAL.value) -> MergeRecord:
    return MergeRecord(repo="owner/repo", pr_number=42, head_sha="abc123", state=state)


class _FakeGitHub:
    def __init__(self, outcome: MergeOutcome) -> None:
        self._outcome = outcome

    async def get_pr(self, repo, pr_number: int) -> dict:
        return {"mergeable": True, "mergeable_state": "clean"}

    async def merge_pr(self, repo, pr_number: int, *, sha: str = "") -> MergeOutcome:
        return self._outcome


@pytest.fixture
def patched(monkeypatch):
    """把路由依赖换成假的，并捕获回发到会话的消息。"""
    from app.routes import feishu

    store = MergeStore()
    sent: list[tuple[str, str]] = []

    async def _fake_safe_send(target: str, text: str, trace_id: str) -> None:
        sent.append((target, text))

    async def _noop_audit(**kwargs) -> None:
        return None

    monkeypatch.setattr(feishu, "merge_store", store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_safe_send)
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_human_decision", _noop_audit
    )
    return feishu, store, sent


@pytest.mark.asyncio
async def test_approval_replies_with_merge_result(monkeypatch, patched) -> None:
    feishu, store, sent = patched
    record = _make_record()
    store.save(record)
    monkeypatch.setattr(
        feishu,
        "build_github_client",
        lambda: _FakeGitHub(MergeOutcome(merged=True, status="merged", sha="deadbeef")),
    )

    response = await feishu._handle_merge_approval(
        hitl_id=record.task_key,
        action="approve",
        operator_id="ou_user",
        session_id="oc_chat",
        trace_id=record.task_key,
    )

    assert response.status_code == 200
    assert len(sent) == 1
    target, text = sent[0]
    assert target == "oc_chat"
    assert "已合并" in text
    assert "deadbeef" in text


@pytest.mark.asyncio
async def test_rejection_replies_that_nothing_merged(monkeypatch, patched) -> None:
    feishu, store, sent = patched
    record = _make_record()
    store.save(record)
    monkeypatch.setattr(
        feishu,
        "build_github_client",
        lambda: _FakeGitHub(
            MergeOutcome(merged=False, status="rejected", message="已拒绝")
        ),
    )

    await feishu._handle_merge_approval(
        hitl_id=record.task_key,
        action="reject",
        operator_id="ou_user",
        session_id="oc_chat",
        trace_id=record.task_key,
    )

    assert len(sent) == 1
    assert "已拒绝" in sent[0][1]


@pytest.mark.asyncio
async def test_failure_replies_with_real_reason(monkeypatch, patched) -> None:
    """合并失败必须把 GitHub 的原话回给发起人，而不是让人去日志里找。"""
    feishu, store, sent = patched
    record = _make_record()
    store.save(record)
    monkeypatch.setattr(
        feishu,
        "build_github_client",
        lambda: _FakeGitHub(
            MergeOutcome(
                merged=False, status="sha_mismatch", message="Head branch was modified"
            )
        ),
    )

    await feishu._handle_merge_approval(
        hitl_id=record.task_key,
        action="approve",
        operator_id="ou_user",
        session_id="oc_chat",
        trace_id=record.task_key,
    )

    assert len(sent) == 1
    assert "Head branch was modified" in sent[0][1]
