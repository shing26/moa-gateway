"""合并 worker 一轮内的顺序：CI 轮询 → 卡片 → GitHub 评论审批（ADR-040）。

评论审批必须挂在既有 CI 轮询之后，这样本轮刚进入 ``awaiting_approval`` 的记录
也能在同一轮读到已有评论；同时它不能改变 CI 轮询与卡片发送的既有语义。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.channels.feishu_cards import ApprovalCard
from app.config import settings
from app.merge_worker import MergeWorker
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.notifications.merge_notifier import (
    ApprovalCardDelivery,
    MergeNotifier,
)
from apps.code_review_pipeline.routing.github_client import (
    CI_FAILURE,
    CI_SUCCESS,
    CiCheck,
    CiStatus,
)

_PR_INFO = {
    "title": "feat: enrich approval card",
    "additions": 120,
    "deletions": 7,
    "changed_files": 4,
    "base": {"ref": "main"},
    "html_url": "https://github.com/owner/repo/pull/7",
    "user": {"login": "shing26"},
}


class _OrderedGitHub:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        self._events.append("ci")
        return CiStatus(state=CI_SUCCESS)

    async def get_pr(self, repo, pr_number: int) -> dict[str, Any]:
        return _PR_INFO


class _OrderedNotifier:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def send_approval_card(self, record, pr_details=None) -> ApprovalCardDelivery:
        self._events.append("card")
        return ApprovalCardDelivery(True, "om_ordered")

    async def send_ci_failure_card(self, record) -> None:
        self._events.append("failure")


class _OrderedCommentApproval:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def poll_once(self) -> tuple:
        self._events.append("comment")
        return ()


class _CountingHeartbeat:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls = 0
        self._events = events

    async def __call__(self) -> None:
        self.calls += 1
        if self._events is not None:
            self._events.append("heartbeat")


async def _noop_heartbeat() -> None:
    return None


@pytest.mark.asyncio
async def test_comment_approval_runs_after_ci_poll_and_card() -> None:
    events: list[str] = []
    store = MergeStore()
    store.save(
        MergeRecord(
            repo="owner/repo",
            pr_number=7,
            head_sha="sha7",
            state=MergeState.WATCHING.value,
            bundle_hash="abc123",
        )
    )
    worker = MergeWorker(
        store=store,
        github=_OrderedGitHub(events),
        notifier=_OrderedNotifier(events),  # type: ignore[arg-type]
        comment_approval=_OrderedCommentApproval(events),
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert events == ["ci", "card", "comment"]
    assert store.list_open()[0].state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_heartbeat_is_written_once_at_end_of_successful_round() -> None:
    """T1：一轮成功后写一次心跳，且写在评论审批之后（证明整轮真的跑完）。"""
    events: list[str] = []
    heartbeat = _CountingHeartbeat(events)
    worker = MergeWorker(
        store=MergeStore(),
        github=_OrderedGitHub(events),
        notifier=_OrderedNotifier(events),  # type: ignore[arg-type]
        comment_approval=_OrderedCommentApproval(events),
        heartbeat=heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert heartbeat.calls == 1
    assert events[-1] == "heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_failure_is_logged_but_does_not_break_round(caplog) -> None:
    """T1：心跳写失败只告警，轮询结果与卡片照常，异常不外泄。"""
    events: list[str] = []

    async def failing_heartbeat() -> None:
        raise ConnectionError("redis down")

    store = MergeStore()
    store.save(
        MergeRecord(
            repo="owner/repo",
            pr_number=8,
            head_sha="sha8",
            state=MergeState.WATCHING.value,
            bundle_hash="abc123",
        )
    )
    worker = MergeWorker(
        store=store,
        github=_OrderedGitHub(events),
        notifier=_OrderedNotifier(events),  # type: ignore[arg-type]
        comment_approval=_OrderedCommentApproval(events),
        heartbeat=failing_heartbeat,
    )

    with caplog.at_level("WARNING", logger="moa.merge_worker"):
        await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert events == ["ci", "card", "comment"]
    assert store.list_open()[0].state == MergeState.AWAITING_APPROVAL.value
    assert "heartbeat write failed" in caplog.text


class _PrGitHub:
    def __init__(
        self, pr_info: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self._pr_info = pr_info
        self._error = error

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        return CiStatus(state=CI_SUCCESS)

    async def get_pr(self, repo, pr_number: int) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return self._pr_info or {}


class _RecordingCardSender:
    def __init__(self, message_id: str = "om_worker_1") -> None:
        self.message_id = message_id
        self.sent_cards: list[ApprovalCard] = []

    async def send_card(self, card: ApprovalCard) -> bool:
        return bool(await self.send_card_with_id(card))

    async def send_card_with_id(self, card: ApprovalCard) -> str:
        self.sent_cards.append(card)
        return self.message_id


class _WorkerSessionStore:
    def __init__(self) -> None:
        self.stored: dict[str, Any] = {}

    def store_hitl(self, session_id: str, request: Any) -> None:
        self.stored[request.trace_id or session_id] = request

    def remove_hitl(self, hitl_id: str) -> None:
        self.stored.pop(hitl_id, None)


class _DeliveryNotifier:
    def __init__(self, delivery: ApprovalCardDelivery) -> None:
        self._delivery = delivery
        self.details: list[dict[str, Any] | None] = []

    async def send_approval_card(self, record, pr_details=None):
        self.details.append(pr_details)
        return self._delivery

    async def send_ci_failure_card(self, record) -> None:
        return None


class _SpyStore(MergeStore):
    def __init__(self) -> None:
        super().__init__()
        self.card_message_writes: list[tuple[tuple[str, int, str], str]] = []

    def set_card_message_id(
        self, identity: tuple[str, int, str], message_id: str
    ) -> None:
        self.card_message_writes.append((identity, message_id))
        super().set_card_message_id(identity, message_id)


def _watching_store(
    pr_number: int = 7, store: MergeStore | None = None
) -> MergeStore:
    store = store or MergeStore()
    store.save(
        MergeRecord(
            repo="owner/repo",
            pr_number=pr_number,
            head_sha="deadbeef1234567890",
            state=MergeState.WATCHING.value,
        )
    )
    return store


@pytest.mark.asyncio
async def test_approval_card_enriched_from_get_pr() -> None:
    """T1：worker 用 get_pr 富化卡片，不再让人签一个光秃秃的 sha。"""
    store = _watching_store()
    sender = _RecordingCardSender()
    notifier = MergeNotifier(
        card_sender=sender,  # type: ignore[arg-type]
        default_target="chat_123",
        session_store=_WorkerSessionStore(),
    )
    worker = MergeWorker(
        store=store,
        github=_PrGitHub(pr_info=_PR_INFO),
        notifier=notifier,
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert len(sender.sent_cards) == 1
    text = sender.sent_cards[0].agent_output
    assert "标题: feat: enrich approval card" in text
    assert "变更: 4 files  +120 −7  → main" in text
    assert "作者: shing26" in text
    assert "PR  : #7 https://github.com/owner/repo/pull/7" in text
    assert "SHA : deadbeef1234" in text
    assert "批准后: 以 merge commit 合并到 main，并写入审计（审批人 + 时间）" in text


@pytest.mark.asyncio
async def test_get_pr_failure_falls_back_and_still_sends() -> None:
    """T1：get_pr 抛异常时照常发卡，正文降级为旧三行。"""
    store = _watching_store()
    sender = _RecordingCardSender()
    notifier = MergeNotifier(
        card_sender=sender,  # type: ignore[arg-type]
        default_target="chat_123",
        session_store=_WorkerSessionStore(),
    )
    worker = MergeWorker(
        store=store,
        github=_PrGitHub(error=RuntimeError("github down")),
        notifier=notifier,
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert len(sender.sent_cards) == 1
    assert sender.sent_cards[0].agent_output == (
        "PR: owner/repo#7\n"
        "SHA: deadbeef1234567890\n"
        "CI: success"
    )
    assert store.get(("owner/repo", 7, "deadbeef1234567890")).state == (
        MergeState.AWAITING_APPROVAL.value
    )


@pytest.mark.asyncio
async def test_get_pr_missing_field_falls_back_and_still_sends() -> None:
    """T1：字段缺失同样降级，不因展示信息不全漏发审批。"""
    store = _watching_store()
    sender = _RecordingCardSender()
    partial = dict(_PR_INFO)
    partial.pop("user")
    notifier = MergeNotifier(
        card_sender=sender,  # type: ignore[arg-type]
        default_target="chat_123",
        session_store=_WorkerSessionStore(),
    )
    worker = MergeWorker(
        store=store,
        github=_PrGitHub(pr_info=partial),
        notifier=notifier,
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert len(sender.sent_cards) == 1
    assert sender.sent_cards[0].agent_output.startswith("PR: owner/repo#7\n")
    assert "标题:" not in sender.sent_cards[0].agent_output


@pytest.mark.asyncio
async def test_message_id_is_written_back_after_successful_send() -> None:
    """T2：发送成功后把 message id 写回记录，供终态改卡使用。"""
    store = _watching_store(store=_SpyStore())
    notifier = _DeliveryNotifier(ApprovalCardDelivery(True, "om_42"))
    worker = MergeWorker(
        store=store,
        github=_PrGitHub(pr_info=_PR_INFO),
        notifier=notifier,  # type: ignore[arg-type]
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    record = store.get(("owner/repo", 7, "deadbeef1234567890"))
    assert record is not None
    assert record.card_message_id == "om_42"
    assert store.card_message_writes == [
        (("owner/repo", 7, "deadbeef1234567890"), "om_42")
    ]
    assert len(notifier.details) == 1
    assert notifier.details[0] is not None


@pytest.mark.asyncio
async def test_send_failure_leaves_card_message_id_unchanged() -> None:
    """T2：发送失败时记录不变，不写一个假的 message id。"""
    store = _watching_store(store=_SpyStore())
    worker = MergeWorker(
        store=store,
        github=_PrGitHub(pr_info=_PR_INFO),
        notifier=_DeliveryNotifier(ApprovalCardDelivery(False, "")),  # type: ignore[arg-type]
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    record = store.get(("owner/repo", 7, "deadbeef1234567890"))
    assert record is not None
    assert record.card_message_id == ""
    assert store.card_message_writes == []


class _TriageGitHub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        self.calls.append("get_ci_status")
        return CiStatus(
            state=CI_FAILURE,
            checks=(
                CiCheck(
                    name="test",
                    conclusion="failure",
                    url="https://github.com/owner/repo/runs/123",
                ),
            ),
        )

    async def get_check_run_status(self, repo, ref: str) -> CiStatus:
        self.calls.append("get_check_run_status")
        return CiStatus(
            state=CI_FAILURE,
            checks=(
                CiCheck(
                    name="test",
                    conclusion="failure",
                    url="https://github.com/owner/repo/runs/123",
                ),
            ),
        )

    async def list_workflow_runs_for_sha(self, repo, head_sha: str):
        self.calls.append("list_workflow_runs_for_sha")
        return [
            {
                "id": 123,
                "run_number": 1,
                "run_attempt": 1,
                "conclusion": "failure",
                "name": "CI",
            }
        ]

    async def list_workflow_jobs(self, repo, run_id: int):
        self.calls.append("list_workflow_jobs")
        return [
            {
                "id": 9,
                "name": "test",
                "conclusion": "failure",
                "html_url": "https://github.com/owner/repo/runs/123/job/9",
            }
        ]

    async def get_job_log(self, repo, job_id: int) -> str:
        self.calls.append("get_job_log")
        return "FAILED tests/unit/test_x.py::test_y - AssertionError: 1 == 2"

    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected GitHub call: {name}")


@pytest.mark.asyncio
async def test_d2_a5_triage_only_uses_the_four_read_methods(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "digest_target", "")
    monkeypatch.setattr(settings, "code_review_merge_channel", "")
    store = _watching_store(pr_number=11)
    github = _TriageGitHub()
    sender = _RecordingCardSender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_123")
    worker = MergeWorker(
        store=store,
        github=github,
        notifier=notifier,
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert github.calls == [
        "get_ci_status",
        "get_check_run_status",
        "list_workflow_runs_for_sha",
        "list_workflow_jobs",
        "get_job_log",
    ]
    assert len(sender.sent_cards) == 1
    assert "结论: test_failure（1 个失败 job）" in sender.sent_cards[0].agent_output


class _TwoRecordGitHub:
    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        if ref == "sha-red":
            return CiStatus(state=CI_FAILURE)
        return CiStatus(state=CI_SUCCESS)


class _ExplodingCardNotifier:
    def __init__(self) -> None:
        self.approvals: list[Any] = []

    async def send_approval_card(self, record, pr_details=None):
        self.approvals.append(record)
        return ApprovalCardDelivery(True, "om_ok")

    async def send_ci_failure_card(self, record, triage=None) -> None:
        raise RuntimeError("failure card boom")

    async def send_digest_card(self, text: str, *, target=None) -> str:
        raise RuntimeError("digest boom")


@pytest.mark.asyncio
async def test_d3_a1_card_exceptions_do_not_block_other_cards(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "digest_enabled", True)
    monkeypatch.setattr(settings, "digest_hour", 0)
    monkeypatch.setattr(settings, "digest_target", "chat_digest")
    monkeypatch.setattr(settings, "digest_state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(
        settings, "delivery_run_record_dir", str(tmp_path / "runs")
    )
    store = MergeStore()
    store.save(
        MergeRecord(
            repo="owner/repo",
            pr_number=10,
            head_sha="sha-green",
            state=MergeState.WATCHING.value,
        )
    )
    store.save(
        MergeRecord(
            repo="owner/repo",
            pr_number=11,
            head_sha="sha-red",
            state=MergeState.WATCHING.value,
        )
    )
    notifier = _ExplodingCardNotifier()
    worker = MergeWorker(
        store=store,
        github=_TwoRecordGitHub(),
        notifier=notifier,  # type: ignore[arg-type]
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert [record.pr_number for record in notifier.approvals] == [10]
    assert store.get(("owner/repo", 11, "sha-red")).state == (
        MergeState.CI_FAILED.value
    )
