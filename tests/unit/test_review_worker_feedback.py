from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.code_review_pipeline.schemas.feedback import persisted_finding_from_result
from apps.code_review_pipeline.schemas.pipeline import Finding
from apps.code_review_pipeline.task_queue import TaskMessage
from apps.code_review_pipeline.storage.review_store import ReviewRecord, ReviewStore
from apps.code_review_pipeline.task_state import TaskState
from app.worker import TaskWorker


def _message() -> TaskMessage:
    return TaskMessage(
        message_id="1-0",
        task_key="owner/repo#7@sha",
        repo="owner/repo",
        pr_number=7,
        head_sha="sha",
    )


def _result(*, findings: tuple[object, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        trace_id="cr_owner/repo#7@sha",
        pr=SimpleNamespace(
            repo="owner/repo",
            pr_number=7,
            head_sha="sha",
            base_sha="base",
            author="dev",
            title="title",
            html_url="https://example.test/pr/7",
            diff_url="https://example.test/pr/7.diff",
            changed_files=(),
        ),
        triage=None,
        static_analysis=None,
        semantic_review=None,
        test_coverage=None,
        report=None,
        overall_need_human_review=False,
        persisted_findings=findings,
        critic_metrics={},
    )


@pytest.mark.asyncio
async def test_worker_does_not_notify_when_persistence_fails(monkeypatch) -> None:
    from app import worker

    result = _result()

    class _Pipeline:
        @classmethod
        def from_env(cls):
            return cls()

        async def run(self, _event):
            return result.pr, result

    class _Store:
        def start_review_run(self, *_args, **_kwargs):
            return None

        def save_review(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    class _Notifier:
        calls = 0

        @classmethod
        def from_env(cls):
            return cls()

        async def send_finding_cards(self, **_kwargs):
            self.__class__.calls += 1
            return 1

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "apps.code_review_pipeline.agents.code_review_pipeline.CodeReviewPipeline",
        _Pipeline,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_agent_rows",
        _noop,
    )
    monkeypatch.setattr(worker, "build_review_store", lambda: _Store())
    monkeypatch.setattr(
        "apps.code_review_pipeline.notifications.feishu_notifier.FeishuReviewNotifier",
        _Notifier,
    )
    monkeypatch.setattr(
        "app.config.settings.code_review_finding_feedback_enabled",
        True,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await worker.review_processor(_message())
    assert _Notifier.calls == 0


@pytest.mark.asyncio
async def test_worker_sends_control_card_when_feedback_mode_has_zero_findings(
    monkeypatch,
) -> None:
    from app import worker

    result = _result(findings=())

    class _Pipeline:
        @classmethod
        def from_env(cls):
            return cls()

        async def run(self, _event):
            return result.pr, result

    class _Store:
        def start_review_run(self, *_args, **_kwargs):
            return None

        def save_review(self, *_args, **_kwargs):
            return None

        def list_findings(self, _trace_id):
            return ()

        def get_task_state(self, _identity):
            return "running"

        def transition_task(self, *_args, **_kwargs):
            return "waiting_approval"

    calls: list[tuple[str, tuple[object, ...]]] = []

    class _Notifier:
        is_configured = True

        @classmethod
        def from_env(cls):
            return cls()

        async def send_finding_cards(self, **kwargs):
            calls.append(("cards", kwargs["findings"]))
            return 1

        @staticmethod
        def expected_finding_card_count(_findings):
            return 1

        async def send_summary(self, _notification):
            calls.append(("summary", ()))

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "apps.code_review_pipeline.agents.code_review_pipeline.CodeReviewPipeline",
        _Pipeline,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_agent_rows",
        _noop,
    )
    monkeypatch.setattr(worker, "build_review_store", lambda: _Store())
    monkeypatch.setattr(
        "apps.code_review_pipeline.notifications.feishu_notifier.FeishuReviewNotifier",
        _Notifier,
    )
    monkeypatch.setattr(
        "app.config.settings.code_review_finding_feedback_enabled",
        True,
    )

    outcome = await worker.review_processor(_message())

    assert outcome == "waiting_approval"
    assert calls == [("cards", ())]


@pytest.mark.asyncio
async def test_worker_accepts_waiting_state_already_written_before_notification(
    monkeypatch,
) -> None:
    store = ReviewStore()
    record = ReviewRecord(
        trace_id="cr_owner/repo#7@sha",
        repo="owner/repo",
        pr_number=7,
        head_sha="sha",
        author="dev",
        findings_count=0,
        need_human_review=True,
        raw={},
    )
    store.save(record)
    identity = record.repo, record.pr_number, record.head_sha
    assert store.claim_task(identity)
    store.transition_task(identity, "request_approval")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_lifecycle",
        _noop,
    )
    worker = TaskWorker(queue=SimpleNamespace(), store=store, processor=_noop)

    await worker._apply_outcome(_message(), TaskState.WAITING_APPROVAL.value)

    assert store.get_task_state(identity) == TaskState.WAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_worker_sends_cards_from_deduplicated_persisted_snapshot(monkeypatch) -> None:
    from app import worker

    finding = Finding(
        id="raw-1",
        severity="high",
        category="logic",
        file="app/service.py",
        line=10,
        title="Wrong retry boundary",
        description="The last retry is skipped.",
        suggestion="Use an inclusive boundary.",
        confidence=0.9,
        evidence=("diff:app/service.py:10",),
    )
    trace_id = "cr_owner/repo#7@sha"
    candidate = persisted_finding_from_result(
        trace_id=trace_id,
        agent="report",
        finding=finding,
        status="candidate",
    )
    published = persisted_finding_from_result(
        trace_id=trace_id,
        agent="report",
        finding=finding,
        status="published",
    )
    result = _result(findings=(candidate, published))
    store = ReviewStore()
    store.save(worker.record_from_result(result))
    assert store.claim_task(_message().identity)

    class _Pipeline:
        @classmethod
        def from_env(cls):
            return cls()

        async def run(self, _event):
            return result.pr, result

    delivered: list[tuple[object, ...]] = []

    class _Notifier:
        is_configured = True

        @classmethod
        def from_env(cls):
            return cls()

        async def send_finding_cards(self, **kwargs):
            delivered.append(kwargs["findings"])
            return 1

        @staticmethod
        def expected_finding_card_count(_findings):
            return 1

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "apps.code_review_pipeline.agents.code_review_pipeline.CodeReviewPipeline",
        _Pipeline,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_agent_rows",
        _noop,
    )
    monkeypatch.setattr(worker, "build_review_store", lambda: store)
    monkeypatch.setattr(
        "apps.code_review_pipeline.notifications.feishu_notifier.FeishuReviewNotifier",
        _Notifier,
    )
    monkeypatch.setattr(
        "app.config.settings.code_review_finding_feedback_enabled",
        True,
    )

    outcome = await worker.review_processor(_message())

    assert outcome == TaskState.WAITING_APPROVAL.value
    assert delivered == [(published,)]
    assert [item.status for item in store.list_finding_events(trace_id)] == [
        "candidate",
        "published",
    ]


@pytest.mark.asyncio
async def test_worker_stays_running_when_card_delivery_fails(monkeypatch) -> None:
    from app import worker

    result = _result()
    store = ReviewStore()
    store.save(worker.record_from_result(result))
    assert store.claim_task(_message().identity)

    class _Pipeline:
        @classmethod
        def from_env(cls):
            return cls()

        async def run(self, _event):
            return result.pr, result

    class _Notifier:
        is_configured = True

        @classmethod
        def from_env(cls):
            return cls()

        async def send_finding_cards(self, **_kwargs):
            raise RuntimeError("feishu unavailable")

        @staticmethod
        def expected_finding_card_count(_findings):
            return 1

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "apps.code_review_pipeline.agents.code_review_pipeline.CodeReviewPipeline",
        _Pipeline,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_agent_rows",
        _noop,
    )
    monkeypatch.setattr(worker, "build_review_store", lambda: store)
    monkeypatch.setattr(
        "apps.code_review_pipeline.notifications.feishu_notifier.FeishuReviewNotifier",
        _Notifier,
    )
    monkeypatch.setattr(
        "app.config.settings.code_review_finding_feedback_enabled",
        True,
    )

    with pytest.raises(RuntimeError, match="feishu unavailable"):
        await worker.review_processor(_message())

    assert store.get_task_state(_message().identity) == TaskState.RUNNING.value


@pytest.mark.asyncio
async def test_worker_fails_closed_when_feedback_destination_is_missing(
    monkeypatch,
) -> None:
    from app import worker

    result = _result()
    store = ReviewStore()
    store.save(worker.record_from_result(result))
    assert store.claim_task(_message().identity)

    class _Pipeline:
        @classmethod
        def from_env(cls):
            return cls()

        async def run(self, _event):
            return result.pr, result

    class _Notifier:
        is_configured = False

        @classmethod
        def from_env(cls):
            return cls()

        async def send_finding_cards(self, **_kwargs):
            raise AssertionError("unconfigured notifier must not report a send")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "apps.code_review_pipeline.agents.code_review_pipeline.CodeReviewPipeline",
        _Pipeline,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_agent_rows",
        _noop,
    )
    monkeypatch.setattr(worker, "build_review_store", lambda: store)
    monkeypatch.setattr(
        "apps.code_review_pipeline.notifications.feishu_notifier.FeishuReviewNotifier",
        _Notifier,
    )
    monkeypatch.setattr(
        "app.config.settings.code_review_finding_feedback_enabled",
        True,
    )

    with pytest.raises(RuntimeError, match="CODE_REVIEW_FEISHU_CHANNEL"):
        await worker.review_processor(_message())

    assert store.get_task_state(_message().identity) == TaskState.RUNNING.value


@pytest.mark.asyncio
async def test_worker_fails_when_only_part_of_finding_cards_are_delivered(
    monkeypatch,
) -> None:
    from app import worker

    finding = persisted_finding_from_result(
        trace_id="cr_owner/repo#7@sha",
        agent="report",
        finding=Finding(
            id="raw-1",
            severity="high",
            category="logic",
            file="app/service.py",
            line=10,
            title="Wrong retry boundary",
            description="The last retry is skipped.",
            suggestion="Use an inclusive boundary.",
            confidence=0.9,
            evidence=("diff:app/service.py:10",),
        ),
    )
    result = _result(findings=(finding,))
    store = ReviewStore()
    store.save(worker.record_from_result(result))
    assert store.claim_task(_message().identity)

    class _Pipeline:
        @classmethod
        def from_env(cls):
            return cls()

        async def run(self, _event):
            return result.pr, result

    class _Notifier:
        is_configured = True

        @classmethod
        def from_env(cls):
            return cls()

        @staticmethod
        def expected_finding_card_count(_findings):
            return 1

        async def send_finding_cards(self, **_kwargs):
            return 0

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "apps.code_review_pipeline.agents.code_review_pipeline.CodeReviewPipeline",
        _Pipeline,
    )
    monkeypatch.setattr(
        "apps.code_review_pipeline.task_audit.record_agent_rows",
        _noop,
    )
    monkeypatch.setattr(worker, "build_review_store", lambda: store)
    monkeypatch.setattr(
        "apps.code_review_pipeline.notifications.feishu_notifier.FeishuReviewNotifier",
        _Notifier,
    )
    monkeypatch.setattr(
        "app.config.settings.code_review_finding_feedback_enabled",
        True,
    )

    with pytest.raises(RuntimeError, match="cards incomplete"):
        await worker.review_processor(_message())

    assert store.get_task_state(_message().identity) == TaskState.RUNNING.value
