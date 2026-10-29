from __future__ import annotations

from typing import Any

import pytest

from apps.code_review_pipeline.notifications.feishu_notifier import FindingReviewCard
from apps.code_review_pipeline.schemas.feedback import (
    FindingFeedback,
    persisted_finding_from_result,
)
from apps.code_review_pipeline.schemas.pipeline import Finding
from apps.code_review_pipeline.storage.review_store import ReviewRecord, ReviewStore


def _store_with_review(
    *,
    waiting_approval: bool = True,
    review_run_id: str = "",
) -> tuple[ReviewStore, ReviewRecord, Any]:
    store = ReviewStore()
    record = ReviewRecord(
        trace_id="cr_owner/repo#9@sha-9",
        repo="owner/repo",
        pr_number=9,
        head_sha="sha-9",
        author="dev",
        findings_count=1,
        need_human_review=True,
        raw={},
        review_run_id=review_run_id,
    )
    finding = persisted_finding_from_result(
        trace_id=record.trace_id,
        agent="report",
        finding=Finding(
            id="f1",
            severity="high",
            category="logic",
            file="app/x.py",
            line=4,
            title="Missing guard",
            description="The branch has no guard.",
            suggestion="Add a guard.",
            evidence=("diff:app/x.py:4",),
        ),
    )
    store.save_review(record, (finding,))
    identity = (record.repo, record.pr_number, record.head_sha)
    assert store.claim_task(identity) is True
    if waiting_approval:
        store.transition_task(identity, "request_approval")
    return store, record, finding


def test_finding_card_has_five_disposition_buttons_and_publish_control() -> None:
    _store, record, finding = _store_with_review()
    card = FindingReviewCard(
        trace_id=record.trace_id,
        repo=record.repo,
        pr_number=record.pr_number,
        head_sha=record.head_sha,
        findings=(finding,),
        session_id=record.trace_id,
        feedback_by_uid={finding.finding_uid: "accepted_actionable"},
    )
    values = [
        action["value"]
        for element in card.to_card_payload()["elements"]
        if element.get("tag") == "action"
        for action in element["actions"]
    ]
    assert len(values) == 5
    assert {item["disposition"] for item in values} == {
        "accepted_actionable",
        "accepted_non_actionable",
        "false_positive",
        "duplicate",
        "already_known",
    }
    assert all(item["page"] == 1 and item["total_pages"] == 1 for item in values)
    texts = [
        element.get("content", "")
        for element in card.to_card_payload()["elements"]
        if element.get("tag") == "markdown"
    ]
    assert any("当前标签：`accepted_actionable`" in text for text in texts)
    payload = card.to_message_payload()
    assert payload["uuid"] == card.to_message_payload()["uuid"]
    assert payload["uuid"] != FindingReviewCard(
        trace_id=record.trace_id,
        repo=record.repo,
        pr_number=record.pr_number,
        head_sha=record.head_sha,
        findings=(finding,),
        page=2,
        total_pages=2,
        session_id=record.trace_id,
    ).to_message_payload()["uuid"]
    control = FindingReviewCard(
        trace_id=record.trace_id,
        repo=record.repo,
        pr_number=record.pr_number,
        head_sha=record.head_sha,
        control=True,
    )
    control_values = [
        action["value"]
        for element in control.to_card_payload()["elements"]
        if element.get("tag") == "action"
        for action in element["actions"]
    ]
    assert {item["action"] for item in control_values} == {
        "review_publish",
        "review_reject",
    }
    assert control.to_message_payload()["uuid"] != payload["uuid"]


@pytest.mark.asyncio
async def test_feedback_route_rejects_stale_sha_and_incomplete_publish(
    monkeypatch,
) -> None:
    from app.routes import feishu

    store, record, finding = _store_with_review()
    sent: list[str] = []

    async def _fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append(text)

    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_send)

    await feishu._process_review_card_action(
        action="review_finding_feedback",
        value={
            "trace_id": record.trace_id,
            "head_sha": "old-sha",
            "finding_uid": finding.finding_uid,
            "disposition": "accepted_actionable",
        },
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-stale",
    )
    assert store.feedback_status(record.trace_id).complete is False
    assert any("过期" in text for text in sent)

    await feishu._process_review_card_action(
        action="review_publish",
        value={"trace_id": record.trace_id, "head_sha": record.head_sha},
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-incomplete",
    )
    assert any("未标注" in text for text in sent)


@pytest.mark.asyncio
async def test_duplicate_feedback_is_append_only_and_successful_publish_is_comment(
    monkeypatch,
) -> None:
    from app.routes import feishu

    store, record, finding = _store_with_review(waiting_approval=False)
    sent: list[str] = []
    posted: list[str] = []
    control_calls: list[str] = []
    card_updates: list[dict[str, Any]] = []

    async def _fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append(text)

    class _Notifier:
        async def send_publish_control(self, **kwargs):
            control_calls.append(kwargs["trace_id"])
            return True

        async def update_feedback_card(self, **kwargs):
            card_updates.append(kwargs)
            return True

    class _GitHub:
        async def list_reviews(self, repo, pr_number):
            return []

        async def create_review(self, repo, pr_number, body, *, event="COMMENT"):
            posted.append(body)
            assert event == "COMMENT"
            return {"id": 88}

    async def _noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_send)
    monkeypatch.setattr(feishu, "build_github_client", lambda: _GitHub())
    monkeypatch.setattr(feishu.settings, "code_review_publish_dry_run", False)
    monkeypatch.setattr(
        "apps.code_review_pipeline.approval.record_human_decision",
        _noop_audit,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.approval.record_lifecycle",
        _noop_audit,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.notifications.feishu_notifier.FeishuReviewNotifier.from_env",
        classmethod(lambda cls: _Notifier()),
    )

    value = {
        "trace_id": record.trace_id,
        "head_sha": record.head_sha,
        "finding_uid": finding.finding_uid,
        "disposition": "accepted_actionable",
        "reason_code": "",
    }
    await feishu._process_review_card_action(
        action="review_finding_feedback",
        value=value,
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-human-1",
        message_id="om_card_1",
    )
    assert store.feedback_status(record.trace_id).complete is True
    assert control_calls == [record.trace_id]
    await feishu._process_review_card_action(
        action="review_finding_feedback",
        value=value,
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-human-1",
        message_id="om_card_1",
    )
    await feishu._process_review_card_action(
        action="review_publish",
        value={"trace_id": record.trace_id, "head_sha": record.head_sha},
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-publish",
    )
    await feishu._process_review_card_action(
        action="review_publish",
        value={"trace_id": record.trace_id, "head_sha": record.head_sha},
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-publish-repeat",
    )
    assert len(posted) == 1
    assert "Missing guard" in posted[0]
    assert "Add a guard." in posted[0]
    assert any("already_posted" in text for text in sent)
    assert len(store._feedback_events) == 1
    assert card_updates == [
        {
            "message_id": "om_card_1",
            "trace_id": record.trace_id,
            "repo": record.repo,
            "pr_number": record.pr_number,
            "head_sha": record.head_sha,
            "review_run_id": record.review_run_id,
            "findings": (finding,),
            "feedback_by_uid": {finding.finding_uid: "accepted_actionable"},
            "page": 1,
            "total_pages": 1,
        }
    ]


@pytest.mark.asyncio
async def test_publish_repairs_recorded_review_stranded_in_posting(
    monkeypatch,
) -> None:
    from app.routes import feishu

    store, record, _finding = _store_with_review(waiting_approval=False)
    identity = (record.repo, record.pr_number, record.head_sha)
    store.transition_task(identity, "request_approval")
    store.transition_task(identity, "approve")
    store.set_posted_review_id(identity, "99")
    sent: list[str] = []
    github_calls: list[str] = []

    async def _fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append(text)

    class _GitHub:
        async def list_reviews(self, repo, pr_number):
            github_calls.append("list")
            return []

        async def create_review(self, repo, pr_number, body, *, event="COMMENT"):
            github_calls.append("post")
            return {"id": 100}

    async def _noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_send)
    monkeypatch.setattr(feishu, "build_github_client", lambda: _GitHub())
    monkeypatch.setattr(feishu.settings, "code_review_publish_dry_run", False)
    monkeypatch.setattr(
        "apps.code_review_pipeline.approval.record_human_decision",
        _noop_audit,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.approval.record_lifecycle",
        _noop_audit,
    )

    await feishu._process_review_card_action(
        action="review_publish",
        value={"trace_id": record.trace_id, "head_sha": record.head_sha},
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-repair",
    )

    assert store.get_task_state(identity) == "done"
    assert github_calls == []
    assert store.feedback_status(record.trace_id).complete is False
    assert any("already_posted" in text and "99" in text for text in sent)


@pytest.mark.asyncio
async def test_publish_retries_posting_state_after_github_failure(monkeypatch) -> None:
    from app.routes import feishu

    store, record, finding = _store_with_review(waiting_approval=False)
    identity = (record.repo, record.pr_number, record.head_sha)
    store.record_feedback(
        FindingFeedback(
            event_id="evt-complete",
            trace_id=record.trace_id,
            finding_uid=finding.finding_uid,
            disposition="accepted_actionable",
            reason_code="",
            actor="reviewer",
        )
    )
    store.transition_task(identity, "request_approval")
    store.transition_task(identity, "approve")
    posted: list[str] = []
    sent: list[str] = []

    async def _fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append(text)

    class _GitHub:
        async def list_reviews(self, repo, pr_number):
            return []

        async def create_review(self, repo, pr_number, body, *, event="COMMENT"):
            assert event == "COMMENT"
            posted.append(body)
            return {"id": 101}

    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_send)
    monkeypatch.setattr(feishu, "build_github_client", lambda: _GitHub())
    monkeypatch.setattr(feishu.settings, "code_review_publish_dry_run", False)

    await feishu._process_review_card_action(
        action="review_publish",
        value={"trace_id": record.trace_id, "head_sha": record.head_sha},
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-posting-retry",
    )

    assert store.get_task_state(identity) == "done"
    assert store.get_posted_review_id(identity) == "101"
    assert len(posted) == 1
    assert any("已批准并写回 GitHub" in text for text in sent)


@pytest.mark.asyncio
async def test_feedback_without_event_id_is_rejected(monkeypatch) -> None:
    from app.routes import feishu

    store, record, finding = _store_with_review()
    sent: list[str] = []

    async def _fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append(text)

    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_send)

    await feishu._process_review_card_action(
        action="review_finding_feedback",
        value={
            "trace_id": record.trace_id,
            "head_sha": record.head_sha,
            "finding_uid": finding.finding_uid,
            "disposition": "accepted_actionable",
        },
        operator_id="reviewer",
        session_id="chat",
        event_id="",
    )

    assert any("event_id" in text for text in sent)
    assert store.feedback_status(record.trace_id).complete is False


@pytest.mark.asyncio
async def test_reject_requires_complete_feedback(monkeypatch) -> None:
    from app.routes import feishu

    store, record, _finding = _store_with_review()
    sent: list[str] = []

    async def _fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append(text)

    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_send)

    await feishu._process_review_card_action(
        action="review_reject",
        value={"trace_id": record.trace_id, "head_sha": record.head_sha},
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-reject",
    )
    assert store.get_task_state((record.repo, record.pr_number, record.head_sha)) == "waiting_approval"
    assert any("未标注" in text for text in sent)


@pytest.mark.asyncio
async def test_old_review_trace_is_rejected_even_if_its_own_sha_matches(
    monkeypatch,
) -> None:
    from app.routes import feishu

    store, record, finding = _store_with_review()
    newer = ReviewRecord(
        trace_id="cr_owner/repo#9@sha-10",
        repo=record.repo,
        pr_number=record.pr_number,
        head_sha="sha-10",
        author=record.author,
        findings_count=0,
        need_human_review=False,
        raw={},
    )
    store.save(newer)
    sent: list[str] = []

    async def _fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append(text)

    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_send)

    await feishu._process_review_card_action(
        action="review_finding_feedback",
        value={
            "trace_id": record.trace_id,
            "head_sha": record.head_sha,
            "finding_uid": finding.finding_uid,
            "disposition": "accepted_actionable",
        },
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-old-trace",
    )
    assert any("版本已过期" in text for text in sent)
    assert store.feedback_status(record.trace_id).complete is False


@pytest.mark.asyncio
async def test_stale_review_run_is_rejected_within_same_trace(monkeypatch) -> None:
    from app.routes import feishu

    store, record, finding = _store_with_review(review_run_id="run-2")
    sent: list[str] = []

    async def _fake_send(target: str, text: str, trace_id: str) -> None:
        sent.append(text)

    monkeypatch.setattr(feishu, "_get_review_store", lambda: store)
    monkeypatch.setattr(feishu, "_safe_send", _fake_send)

    await feishu._process_review_card_action(
        action="review_finding_feedback",
        value={
            "trace_id": record.trace_id,
            "head_sha": record.head_sha,
            "review_run_id": "run-1",
            "finding_uid": finding.finding_uid,
            "disposition": "accepted_actionable",
        },
        operator_id="reviewer",
        session_id="chat",
        event_id="evt-stale-run",
    )

    assert any("运行版本已过期" in text for text in sent)
    assert store.feedback_status(record.trace_id).complete is False
