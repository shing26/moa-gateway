"""推送审批卡侧车登记与过期巡检（契约 T1/T8/T9/T10）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apps.code_review_pipeline.delivery.push_approval_registry import (
    PENDING_DIR_NAME,
    clear_pending,
    list_pending,
    pending_path,
    sweep_push_cards,
    write_pending,
)

_BASE = 1_764_000_000.0


def _write(
    root: Path,
    *,
    hitl_id: str = "hitl-1",
    created_at: float = _BASE,
    expires_at: float = _BASE + 1800,
    message_id: str = "om_card_1",
    run_id: str = "run-1",
) -> Path:
    return write_pending(
        root,
        hitl_id=hitl_id,
        run_id=run_id,
        repo="owner/repo",
        branch="moa/branch",
        card_message_id=message_id,
        target="oc_chat",
        created_at=created_at,
        expires_at=expires_at,
    )


class _Sender:
    def __init__(self, *, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.updates: list[tuple[str, Any]] = []

    async def update_card(self, message_id: str, card: Any) -> bool:
        self.updates.append((message_id, card))
        if self.error is not None:
            raise self.error
        return self.result


def test_sidecar_roundtrip_and_idempotent_clear(tmp_path: Path) -> None:
    """T1：写→列→清往返；重复清除不抛。"""
    path = _write(tmp_path)

    assert path.parent.name == PENDING_DIR_NAME
    assert path == tmp_path / PENDING_DIR_NAME / "hitl-1.json"
    [record] = list_pending(tmp_path)
    assert record["schema_version"] == 1
    assert record["hitl_id"] == "hitl-1"
    assert record["run_id"] == "run-1"
    assert record["repo"] == "owner/repo"
    assert record["branch"] == "moa/branch"
    assert record["card_message_id"] == "om_card_1"
    assert record["target"] == "oc_chat"
    assert record["created_at"] == _BASE
    assert record["expires_at"] == _BASE + 1800
    assert record["state"] == "pending"

    clear_pending(tmp_path, "hitl-1")
    assert list_pending(tmp_path) == []
    clear_pending(tmp_path, "hitl-1")  # 幂等


@pytest.mark.parametrize("hitl_id", ["..", "../escape", "a/b", "", "a b"])
def test_invalid_hitl_id_is_rejected(tmp_path: Path, hitl_id: str) -> None:
    """T1：非法 hitl_id 不能成为侧车文件名。"""
    with pytest.raises(ValueError):
        pending_path(tmp_path, hitl_id)
    with pytest.raises(ValueError):
        _write(tmp_path, hitl_id=hitl_id)
    assert not (tmp_path / PENDING_DIR_NAME).exists()


def test_corrupt_json_is_skipped_without_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """T1：坏 JSON 记 warning 并跳过，巡检与 CLI 都不能崩。"""
    root = tmp_path / PENDING_DIR_NAME
    root.mkdir(parents=True)
    (root / "broken.json").write_text("{not json", encoding="utf-8")
    (root / "not-object.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    _write(tmp_path, hitl_id="good")

    with caplog.at_level("WARNING", logger="moa.delivery.push_approval_registry"):
        records = list_pending(tmp_path)

    assert [record["hitl_id"] for record in records] == ["good"]
    assert "corrupt push approval sidecar" in caplog.text
    assert "non-object push approval sidecar" in caplog.text


@pytest.mark.asyncio
async def test_sweep_expires_after_grace_and_leaves_fresh_card_alone(
    tmp_path: Path,
) -> None:
    """T8：now >= expires_at + grace 才改卡；宽限内不动。"""
    _write(tmp_path, expires_at=_BASE + 60)
    sender = _Sender()

    fresh = await sweep_push_cards(
        directory=tmp_path,
        sender=sender,
        run_state_of=lambda _run_id: "ready_for_push_approval",
        now=_BASE + 60 + 119,
    )

    assert fresh == 0
    assert sender.updates == []
    assert len(list_pending(tmp_path)) == 1

    swept = await sweep_push_cards(
        directory=tmp_path,
        sender=sender,
        run_state_of=lambda _run_id: "ready_for_push_approval",
        now=_BASE + 60 + 120,
    )

    assert swept == 1
    message_id, card = sender.updates[0]
    assert message_id == "om_card_1"
    assert card.hitl_kind == "push_resolved_expired"
    assert "delivery resume --run-id run-1" in card.agent_output
    assert list_pending(tmp_path) == []


@pytest.mark.asyncio
async def test_sweep_expires_when_run_is_no_longer_ready(tmp_path: Path) -> None:
    """T9：run 已离开 ready_for_push_approval → 未到期也收卡。"""
    _write(tmp_path)
    sender = _Sender()

    swept = await sweep_push_cards(
        directory=tmp_path,
        sender=sender,
        run_state_of=lambda _run_id: "abandoned",
        now=_BASE + 1,
    )

    assert swept == 1
    assert sender.updates[0][1].hitl_kind == "push_resolved_expired"
    assert list_pending(tmp_path) == []


@pytest.mark.asyncio
async def test_sweep_run_state_error_falls_back_to_time_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """T9：run_state_of 抛异常/返回 None 不改变“按时间判定”的结论。"""
    _write(tmp_path)
    sender = _Sender()

    def boom(_run_id: str) -> Any:
        raise ConnectionError("run store down")

    with caplog.at_level("WARNING", logger="moa.delivery.push_approval_registry"):
        swept = await sweep_push_cards(
            directory=tmp_path,
            sender=sender,
            run_state_of=boom,
            now=_BASE + 1,
        )

    assert swept == 0
    assert sender.updates == []
    assert len(list_pending(tmp_path)) == 1
    assert "failed to read run state" in caplog.text

    swept = await sweep_push_cards(
        directory=tmp_path,
        sender=sender,
        run_state_of=lambda _run_id: None,
        now=_BASE + 1,
    )
    assert swept == 0
    assert sender.updates == []


@pytest.mark.asyncio
async def test_sweep_keeps_sidecar_when_card_update_fails(tmp_path: Path) -> None:
    """T10：改卡失败保留侧车，下一轮再试。"""
    _write(tmp_path, expires_at=_BASE + 60)
    sender = _Sender(result=False)

    swept = await sweep_push_cards(
        directory=tmp_path,
        sender=sender,
        run_state_of=lambda _run_id: "ready_for_push_approval",
        now=_BASE + 60 + 120,
    )

    assert swept == 0
    assert len(sender.updates) == 1
    assert len(list_pending(tmp_path)) == 1


@pytest.mark.asyncio
async def test_sweep_discards_after_max_age_even_if_update_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """T10：超过 7 天无论改卡成败都丢弃，避免死 message id 永远重试。"""
    _write(tmp_path, created_at=_BASE, expires_at=_BASE + 60)
    sender = _Sender(error=RuntimeError("feishu down"))

    with caplog.at_level("WARNING", logger="moa.delivery.push_approval_registry"):
        swept = await sweep_push_cards(
            directory=tmp_path,
            sender=sender,
            run_state_of=lambda _run_id: "ready_for_push_approval",
            now=_BASE + 7 * 24 * 3600,
        )

    assert swept == 0
    assert sender.updates
    assert list_pending(tmp_path) == []
    assert "after max age" in caplog.text


@pytest.mark.asyncio
async def test_sweep_discards_unusable_records_without_raising(tmp_path: Path) -> None:
    """T10：无 message id / 坏时间戳直接丢弃；sender=None 直接返回 0。"""
    _write(tmp_path, hitl_id="no-message", message_id="")
    _write(tmp_path, hitl_id="bad-time", created_at=_BASE, expires_at=_BASE + 60)
    bad_path = pending_path(tmp_path, "bad-time")
    payload = json.loads(bad_path.read_text(encoding="utf-8"))
    payload["expires_at"] = "tomorrow"
    bad_path.write_text(json.dumps(payload), encoding="utf-8")

    sender = _Sender()
    swept = await sweep_push_cards(
        directory=tmp_path,
        sender=sender,
        run_state_of=lambda _run_id: "ready_for_push_approval",
        now=_BASE + 1,
    )

    assert swept == 0
    assert sender.updates == []
    assert list_pending(tmp_path) == []

    _write(tmp_path, hitl_id="still-there")
    assert (
        await sweep_push_cards(
            directory=tmp_path,
            sender=None,
            run_state_of=lambda _run_id: "abandoned",
            now=_BASE + 10_000,
        )
        == 0
    )
    assert len(list_pending(tmp_path)) == 1
