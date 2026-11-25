"""合并 worker 一轮内的顺序：CI 轮询 → 卡片 → GitHub 评论审批（ADR-040）。

评论审批必须挂在既有 CI 轮询之后，这样本轮刚进入 ``awaiting_approval`` 的记录
也能在同一轮读到已有评论；同时它不能改变 CI 轮询与卡片发送的既有语义。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.channels.feishu_cards import ApprovalCard
from app.merge_worker import MergeWorker
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.notifications.merge_notifier import (
    ApprovalCardDelivery,
    MergeNotifier,
)
from apps.code_review_pipeline.routing.github_client import (
    CI_PENDING,
    CI_SUCCESS,
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


class _ExpiryGitHub:
    """待批记录的 CI 已经有结论（绿），poller 对它不做任何推进。"""

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        return CiStatus(state=CI_SUCCESS)

    async def get_pr(self, repo, pr_number: int) -> dict[str, Any]:
        return _PR_INFO


class _PendingGitHub:
    """CI 未出结论：poller 原地不动，便于单独观察过期巡检。"""

    async def get_ci_status(self, repo, ref: str) -> CiStatus:
        return CiStatus(state=CI_PENDING)

    async def get_pr(self, repo, pr_number: int) -> dict[str, Any]:
        return _PR_INFO


class _ExpiryCardSender:
    """记录改卡与新发卡；新卡消息 id 固定，便于断言写回。"""

    def __init__(self, new_message_id: str = "om_new") -> None:
        self.new_message_id = new_message_id
        self.updates: list[tuple[str, ApprovalCard]] = []
        self.sent: list[ApprovalCard] = []

    async def update_card(self, message_id: str, card: ApprovalCard) -> bool:
        self.updates.append((message_id, card))
        return True

    async def send_card_with_id(self, card: ApprovalCard) -> str:
        self.sent.append(card)
        return self.new_message_id

    async def send_card(self, card: ApprovalCard) -> bool:
        self.sent.append(card)
        return bool(self.new_message_id)


class _ExpirySessionStore(_WorkerSessionStore):
    """最小 HITL 存储：可预置存活登记，也可让指定 key 读取时抛异常。"""

    def __init__(
        self,
        *,
        live: set[str] | None = None,
        raises: set[str] | None = None,
    ) -> None:
        super().__init__()
        for key in live or ():
            self.stored[key] = object()
        self.raising = set(raises or ())
        self.lookups: list[str] = []

    def get_hitl(self, hitl_id: str) -> Any:
        self.lookups.append(hitl_id)
        if hitl_id in self.raising:
            raise ConnectionError("redis down")
        return self.stored.get(hitl_id)


class _ListStore:
    """只提供 list_open / set_card_message_id 的最小 store 替身。

    用于覆盖 ``merged`` / ``failed`` 这类终态——内存 ``MergeStore.list_open``
    会把它们过滤掉，而契约要求 worker 自己也不处理。
    """

    def __init__(self, records: list[MergeRecord]) -> None:
        self._records = list(records)
        self.card_message_writes: list[tuple[tuple[str, int, str], str]] = []

    def list_open(self) -> list[MergeRecord]:
        return list(self._records)

    def set_card_message_id(
        self, identity: tuple[str, int, str], message_id: str
    ) -> None:
        self.card_message_writes.append((identity, message_id))


def _awaiting_record(
    *,
    pr_number: int = 7,
    card_message_id: str = "om_old",
) -> MergeRecord:
    return MergeRecord(
        repo="owner/repo",
        pr_number=pr_number,
        head_sha="deadbeef1234567890",
        state=MergeState.AWAITING_APPROVAL.value,
        card_message_id=card_message_id,
    )


def _expiry_worker(
    store,
    github,
    *,
    sender: _ExpiryCardSender | None = None,
    session_store: _ExpirySessionStore | None = None,
) -> tuple[MergeWorker, _ExpiryCardSender, _ExpirySessionStore]:
    sender = sender or _ExpiryCardSender()
    session_store = session_store or _ExpirySessionStore()
    notifier = MergeNotifier(
        card_sender=sender,  # type: ignore[arg-type]
        default_target="chat_123",
        session_store=session_store,
    )
    worker = MergeWorker(
        store=store,
        github=github,
        notifier=notifier,
        heartbeat=_noop_heartbeat,
        session_store=session_store,
    )
    return worker, sender, session_store


@pytest.mark.asyncio
async def test_expired_hitl_refreshes_card_and_keeps_awaiting_state() -> None:
    """T1: HITL 返回 None → 旧卡改 expired，发新卡，写回新 id，状态不变。"""
    record = _awaiting_record()
    store = _SpyStore()
    store.save(record)
    worker, sender, session_store = _expiry_worker(store, _ExpiryGitHub())

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert [message_id for message_id, _card in sender.updates] == ["om_old"]
    old_card = sender.updates[0][1]
    assert "⏳ 审批已过期，已重发新卡" in old_card.agent_output
    assert old_card.hitl_kind == "merge_resolved_failed"
    assert [card.hitl_kind for card in sender.sent] == ["merge_approval"]
    assert sender.sent[0].session_id == record.task_key
    assert store.card_message_writes == [(record.identity, "om_new")]
    refreshed = store.get(record.identity)
    assert refreshed is not None
    assert refreshed.card_message_id == "om_new"
    # 硬约束：绝不能转成 failed —— 那是终态，后续 CI 再绿也拉不回来。
    assert refreshed.state == MergeState.AWAITING_APPROVAL.value
    # 新卡重新登记了 HITL，下一轮不会再被判定为过期。
    assert record.task_key in session_store.stored


@pytest.mark.asyncio
async def test_live_hitl_is_left_alone() -> None:
    """T2: get_hitl 返回登记 → 不改卡、不发卡、不写 id。"""
    record = _awaiting_record()
    store = _SpyStore()
    store.save(record)
    worker, sender, session_store = _expiry_worker(
        store,
        _ExpiryGitHub(),
        session_store=_ExpirySessionStore(live={record.task_key}),
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert session_store.lookups == [record.task_key]
    assert sender.updates == []
    assert sender.sent == []
    assert store.card_message_writes == []


@pytest.mark.asyncio
async def test_hitl_read_error_skips_only_that_record(caplog) -> None:
    """T3: 读取 HITL 抛异常 → 只告警跳过，同轮其他记录照常处理，异常不外泄。"""
    broken = _awaiting_record(pr_number=7)
    healthy = _awaiting_record(pr_number=8, card_message_id="om_old_8")
    store = _SpyStore()
    store.save(broken)
    store.save(healthy)
    worker, sender, session_store = _expiry_worker(
        store,
        _ExpiryGitHub(),
        sender=_ExpiryCardSender(new_message_id="om_new_8"),
        session_store=_ExpirySessionStore(raises={broken.task_key}),
    )

    with caplog.at_level("WARNING", logger="moa.merge_worker"):
        await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert "failed to read HITL entry" in caplog.text
    # Redis 抖动不是过期：那条记录一张卡都不该发。
    assert session_store.lookups.count(broken.task_key) == 1
    broken_record = store.get(broken.identity)
    assert broken_record is not None
    assert broken_record.card_message_id == "om_old"
    # 同一轮的另一条记录仍被处理。
    assert [message_id for message_id, _card in sender.updates] == ["om_old_8"]
    assert [card.session_id for card in sender.sent] == [healthy.task_key]
    healthy_record = store.get(healthy.identity)
    assert healthy_record is not None
    assert healthy_record.card_message_id == "om_new_8"


@pytest.mark.asyncio
async def test_record_without_card_message_id_is_skipped() -> None:
    """T4: 没有卡片消息 id → 跳过，连 HITL 都不查。"""
    record = _awaiting_record(card_message_id="")
    store = _SpyStore()
    store.save(record)
    worker, sender, session_store = _expiry_worker(store, _ExpiryGitHub())

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert session_store.lookups == []
    assert sender.updates == []
    assert sender.sent == []
    assert store.card_message_writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        MergeState.WATCHING.value,
        MergeState.MERGED.value,
        MergeState.FAILED.value,
    ],
)
async def test_non_awaiting_states_are_skipped(state: str) -> None:
    """T5: 非 awaiting_approval（watching/merged/failed）不改卡、不发卡。"""
    record = MergeRecord(
        repo="owner/repo",
        pr_number=7,
        head_sha="deadbeef1234567890",
        state=state,
        card_message_id="om_old",
    )
    store = _ListStore([record])
    worker, sender, session_store = _expiry_worker(store, _PendingGitHub())

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert session_store.lookups == []
    assert sender.updates == []
    assert sender.sent == []
    assert store.card_message_writes == []


class _DegradedSessionStore(_ExpirySessionStore):
    """已降级为进程内存（``REDIS_ENABLE_FALLBACK``）：``None`` 不可信。"""

    def __init__(self) -> None:
        super().__init__()
        self.using_memory = True


class _DegradingSessionStore(_ExpirySessionStore):
    """仿 ``RedisHitlStorage``：降级判定发生在**第一次操作时**。"""

    def __init__(self) -> None:
        super().__init__()
        self.using_memory = False

    def get_hitl(self, hitl_id: str) -> Any:
        value = super().get_hitl(hitl_id)
        self.using_memory = True
        return value


@pytest.mark.asyncio
async def test_degraded_hitl_store_skips_whole_sweep(caplog) -> None:
    """T8: 存储降级时整轮跳过——不改卡、不发卡、不写 id，其余记录也不处理。"""
    first = _awaiting_record(pr_number=7)
    second = _awaiting_record(pr_number=8, card_message_id="om_old_8")
    store = _SpyStore()
    store.save(first)
    store.save(second)
    worker, sender, session_store = _expiry_worker(
        store,
        _ExpiryGitHub(),
        session_store=_DegradedSessionStore(),
    )

    with caplog.at_level("WARNING", logger="moa.merge_worker"):
        await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert "degraded to in-memory" in caplog.text
    # 降级判定在读取任何记录之前：连 get_hitl 都不该调用。
    assert session_store.lookups == []
    assert sender.updates == []
    assert sender.sent == []
    assert store.card_message_writes == []
    for record in (first, second):
        stored = store.get(record.identity)
        assert stored is not None
        assert stored.card_message_id == record.card_message_id
        assert stored.state == MergeState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_hitl_degraded_during_read_is_not_treated_as_expired(caplog) -> None:
    """T8: 读之前报告权威、读完才降级——这个 None 来自内存副本，不是过期。"""
    record = _awaiting_record()
    store = _SpyStore()
    store.save(record)
    worker, sender, session_store = _expiry_worker(
        store,
        _ExpiryGitHub(),
        session_store=_DegradingSessionStore(),
    )

    with caplog.at_level("WARNING", logger="moa.merge_worker"):
        await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert "degraded during read" in caplog.text
    assert session_store.lookups == [record.task_key]
    assert sender.updates == []
    assert sender.sent == []
    assert store.card_message_writes == []
    stored = store.get(record.identity)
    assert stored is not None
    assert stored.card_message_id == "om_old"
    assert stored.state == MergeState.AWAITING_APPROVAL.value
