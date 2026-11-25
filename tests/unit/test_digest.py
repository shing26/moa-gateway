"""Governance digest: once-per-day state, rendering, degradation, CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import cli
from app.channels.feishu_cards import ApprovalCard
from app.config import settings
from app.merge_worker import MergeWorker
from apps.code_review_pipeline.delivery import digest
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord, MergeStore
from apps.code_review_pipeline.notifications.merge_notifier import MergeNotifier


class _Sender:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.sent: list[ApprovalCard] = []
        self.calls = 0
        self.fail_first = fail_first
        self.message_id = "om_digest_1"

    async def send_card_with_id(self, card: ApprovalCard) -> str:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            return ""
        self.sent.append(card)
        return self.message_id

    async def send_card(self, card: ApprovalCard) -> bool:
        return bool(await self.send_card_with_id(card))


async def _noop_heartbeat() -> None:
    return None


class _UnusedGitHub:
    async def get_ci_status(self, repo, ref: str):
        raise AssertionError("no merge records should be polled")


def _configure_digest(
    monkeypatch,
    tmp_path: Path,
    *,
    enabled: bool = True,
    hour: int = 0,
    target: str = "chat_digest",
) -> None:
    monkeypatch.setattr(settings, "digest_enabled", enabled)
    monkeypatch.setattr(settings, "digest_hour", hour)
    monkeypatch.setattr(settings, "digest_target", target)
    monkeypatch.setattr(settings, "digest_state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(
        settings, "delivery_run_record_dir", str(tmp_path / "runs")
    )
    monkeypatch.setattr(settings, "code_review_merge_channel", "")


def _write_run(
    root: Path,
    run_id: str,
    *,
    state: str,
    updated_at: datetime,
    started_at: datetime | None = None,
    failure_code: str | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "trace_id": f"trace-{run_id}",
        "request": {
            "trace_id": f"trace-{run_id}",
            "repo": "owner/repo",
            "base_sha": "base",
            "base_branch": "main",
            "branch": "feat",
            "goal": "goal",
            "title": f"title {run_id}",
        },
        "topology": "single",
        "state": state,
        "started_at": (started_at or updated_at).isoformat(),
        "updated_at": updated_at.isoformat(),
    }
    if failure_code is not None:
        payload["failure"] = {"code": failure_code, "message": "boom"}
    (root / f"{run_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_d1_a1_digest_sent_once_per_day(monkeypatch, tmp_path) -> None:
    _configure_digest(monkeypatch, tmp_path)
    sender = _Sender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_digest")
    worker = MergeWorker(
        store=MergeStore(),
        github=_UnusedGitHub(),
        notifier=notifier,
        heartbeat=_noop_heartbeat,
    )

    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮
    await worker._run_once()  # noqa: SLF001 - 单测直接驱动一轮

    assert len(sender.sent) == 1
    card = sender.sent[0]
    assert card.hitl_kind == "notification"
    assert not any(
        element.get("tag") == "action"
        for element in card.to_card_payload()["elements"]
    )


@pytest.mark.asyncio
async def test_d1_a2_failure_retries_after_fifteen_minutes(
    monkeypatch, tmp_path
) -> None:
    _configure_digest(monkeypatch, tmp_path)
    sender = _Sender(fail_first=True)
    notifier = MergeNotifier(card_sender=sender, default_target="chat_digest")
    worker = MergeWorker(
        store=MergeStore(),
        github=_UnusedGitHub(),
        notifier=notifier,
        heartbeat=_noop_heartbeat,
    )
    now = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
    path = digest.state_path(settings.digest_state_dir)

    await worker._maybe_send_digest(now)  # noqa: SLF001
    state = digest.load_state(path)
    assert state.last_sent_date == ""
    assert state.last_attempt_at == now.isoformat()

    await worker._maybe_send_digest(now + timedelta(minutes=10))  # noqa: SLF001
    assert sender.calls == 1

    await worker._maybe_send_digest(now + timedelta(minutes=16))  # noqa: SLF001
    state = digest.load_state(path)
    assert sender.calls == 2
    assert state.last_sent_date == "2026-11-26"
    assert state.message_id == "om_digest_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,target", [(False, "chat_digest"), (True, "")])
async def test_d1_a3_disabled_or_no_target_does_nothing(
    monkeypatch, tmp_path, enabled: bool, target: str
) -> None:
    _configure_digest(
        monkeypatch, tmp_path, enabled=enabled, target=target
    )
    sender = _Sender()
    notifier = MergeNotifier(card_sender=sender, default_target=target)
    worker = MergeWorker(
        store=MergeStore(),
        github=_UnusedGitHub(),
        notifier=notifier,
        heartbeat=_noop_heartbeat,
    )

    await worker._maybe_send_digest(  # noqa: SLF001
        datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
    )

    assert sender.calls == 0
    assert not digest.state_path(settings.digest_state_dir).exists()


def test_d1_a4_five_sections_line_cap_and_links(tmp_path) -> None:
    now = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
    runs = tmp_path / "runs"
    for index, state in enumerate(
        ["needs_human", "budget_exhausted", "failed", "needs_human"]
    ):
        _write_run(
            runs,
            f"dlv-stuck-{index}",
            state=state,
            updated_at=now - timedelta(hours=index + 1),
            failure_code="budget_exhausted" if index == 1 else None,
        )
    records = [
        MergeRecord(
            repo="owner/repo",
            pr_number=100 + index,
            head_sha=f"sha{index}",
            state=MergeState.AWAITING_APPROVAL.value,
            ci_state="success",
            transitions=(
                {
                    "from": MergeState.WATCHING.value,
                    "to": MergeState.AWAITING_APPROVAL.value,
                    "at": (now - timedelta(hours=index + 1)).isoformat(),
                },
            ),
        )
        for index in range(2)
    ]

    report = digest.build_digest_report(
        now, merge_records=records, run_dir=runs
    )
    lines = report.text.splitlines()

    assert lines[0].startswith("MoA 治理摘要 · 2026-11-26 09:00")
    assert any(line.startswith("待你决定: 2") for line in lines)
    assert any(line.startswith("卡住了: 4") for line in lines)
    assert any(line.startswith("进行中: 0（delivery 0 / merge 0）") for line in lines)
    assert any(line.startswith("近 24h: 新开 4") for line in lines)
    assert len(lines) <= 10
    assert any("… 另有 2 条" in line for line in lines)
    for line in lines:
        if line.startswith("  · "):
            assert "https://" in line or "（无链接）" in line


@pytest.mark.asyncio
async def test_d1_a5_merge_store_failure_still_sends_unavailable(
    monkeypatch, tmp_path
) -> None:
    _configure_digest(monkeypatch, tmp_path)
    sender = _Sender()
    notifier = MergeNotifier(card_sender=sender, default_target="chat_digest")

    class _ExplodingStore:
        def list_open(self):
            raise RuntimeError("merge store down")

    worker = MergeWorker(
        store=_ExplodingStore(),
        github=_UnusedGitHub(),
        notifier=notifier,
        heartbeat=_noop_heartbeat,
    )

    await worker._maybe_send_digest(  # noqa: SLF001
        datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
    )

    assert len(sender.sent) == 1
    text = sender.sent[0].agent_output
    assert "不可用" in text
    assert "待你决定: 0" not in text


def test_d1_a6_missing_and_empty_run_dir(tmp_path) -> None:
    now = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
    missing = digest.build_digest_report(
        now, merge_records=[], run_dir=tmp_path / "missing"
    )
    assert "卡住了: 不可用" in missing.text
    assert "进行中: 不可用" in missing.text
    assert "近 24h: 不可用" in missing.text

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    empty = digest.build_digest_report(
        now, merge_records=[], run_dir=empty_dir
    )
    assert "卡住了: 0" in empty.text
    assert "进行中: 0（delivery 0 / merge 0）" in empty.text
    assert "近 24h: 新开 0 · 合并 0 · 失败 0" in empty.text


def test_in_progress_counts_ci_failed_and_merging(tmp_path) -> None:
    now = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
    runs = tmp_path / "runs"
    _write_run(
        runs,
        "dlv-merging",
        state="merging",
        updated_at=now - timedelta(minutes=5),
    )
    records = [
        MergeRecord(
            repo="owner/repo",
            pr_number=200,
            head_sha="sha-ci-failed",
            state=MergeState.CI_FAILED.value,
            ci_state="failure",
        )
    ]

    report = digest.build_digest_report(
        now, merge_records=records, run_dir=runs
    )

    assert report.in_progress == {
        "count": 2,
        "delivery": 1,
        "merge": 1,
        "unavailable": "",
    }


def test_d1_a7_atomic_write_and_corrupt_state(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state" / "state.json"
    digest.save_state(
        path,
        digest.DigestState(
            last_sent_date="2026-11-26",
            last_attempt_at="2026-11-26T09:00:00+00:00",
            message_id="om_1",
        ),
    )
    assert digest.load_state(path).message_id == "om_1"

    path.write_text("{not json", encoding="utf-8")
    assert digest.load_state(path) == digest.DigestState()

    replacements: list[tuple[Path, Path]] = []
    real_replace = digest.os.replace

    def record_replace(source, target):
        replacements.append((Path(source), Path(target)))
        return real_replace(source, target)

    monkeypatch.setattr(digest.os, "replace", record_replace)
    digest.save_state(path, digest.DigestState(last_sent_date="2026-11-27"))

    assert len(replacements) == 1
    source, target = replacements[0]
    assert source.parent == target.parent == path.parent
    assert not source.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["last_sent_date"] == (
        "2026-11-27"
    )


def test_d1_a8_cli_default_prints_without_state(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure_digest(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "apps.code_review_pipeline.merge_store.build_merge_store",
        lambda: MergeStore(),
    )

    code = cli.cmd_delivery_digest(
        type("Args", (), {"send": False, "json": False})()
    )

    assert code == 0
    assert "MoA 治理摘要" in capsys.readouterr().out
    assert not digest.state_path(settings.digest_state_dir).exists()


def test_d1_a8_cli_send_and_json(monkeypatch, tmp_path, capsys) -> None:
    _configure_digest(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "apps.code_review_pipeline.merge_store.build_merge_store",
        lambda: MergeStore(),
    )
    sender = _Sender()
    from app import deps

    monkeypatch.setattr(deps, "_card_sender", sender)
    monkeypatch.setattr(deps, "init_feishu", lambda: None)

    json_code = cli.cmd_delivery_digest(
        type("Args", (), {"send": False, "json": True})()
    )
    payload = json.loads(capsys.readouterr().out)
    assert json_code == 0
    assert payload["pending"]["count"] == 0
    assert "text" in payload
    assert not digest.state_path(settings.digest_state_dir).exists()

    send_code = cli.cmd_delivery_digest(
        type("Args", (), {"send": True, "json": False})()
    )
    assert send_code == 0
    assert len(sender.sent) == 1
    state = digest.load_state(digest.state_path(settings.digest_state_dir))
    assert state.last_sent_date == datetime.now(UTC).astimezone().date().isoformat()
    assert state.message_id == "om_digest_1"
