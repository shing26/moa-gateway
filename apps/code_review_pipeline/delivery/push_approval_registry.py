"""推送审批卡的侧车登记与过期巡检。

``DeliveryRun`` 的 ``schema_version`` 是严格相等校验，往运行记录里加字段会让
现存记录全部失效。所以推送审批卡的展示状态单独存在运行记录目录下的
``.push-approvals/<hitl_id>.json``：CLI 发卡后登记，收到决定或超时后清除；
merge worker 按侧车里的时间与 run 状态把没人等的旧卡改成诚实的终态。

侧车只是展示层：读写失败只能告警，不能影响审批结论。
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.channels.feishu_cards import ApprovalCard

logger = logging.getLogger("moa.delivery.push_approval_registry")

PENDING_DIR_NAME = ".push-approvals"
PENDING_SCHEMA_VERSION = 1
PUSH_APPROVAL_KIND = "push_approval"
_READY_STATE = "ready_for_push_approval"


def _safe_hitl_id(hitl_id: str) -> str:
    """与 run store 同样的白名单，并额外拒绝 ``.`` / ``..``。

    run store 的清洗允许点号，但 ``..`` 本身是路径穿越；侧车文件名来自
    Redis 回调使用的 hitl id，必须在落盘前拒绝。
    """
    safe = "".join(
        char for char in hitl_id if char.isalnum() or char in {"-", "_", "."}
    )
    if not safe or safe != hitl_id or safe in {".", ".."}:
        raise ValueError(f"invalid hitl_id: {hitl_id!r}")
    return safe


def pending_dir(directory: str | Path) -> Path:
    return Path(directory) / PENDING_DIR_NAME


def pending_path(directory: str | Path, hitl_id: str) -> Path:
    return pending_dir(directory) / f"{_safe_hitl_id(hitl_id)}.json"


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_pending(
    directory: str | Path,
    *,
    hitl_id: str,
    run_id: str,
    repo: str,
    branch: str,
    card_message_id: str,
    target: str,
    created_at: float,
    expires_at: float,
) -> Path:
    """登记一张已发出的推送审批卡。``hitl_id`` 非法时直接抛 ``ValueError``。"""
    path = pending_path(directory, hitl_id)
    payload = {
        "schema_version": PENDING_SCHEMA_VERSION,
        "hitl_id": hitl_id,
        "run_id": run_id,
        "repo": repo,
        "branch": branch,
        "card_message_id": card_message_id,
        "target": target,
        "created_at": float(created_at),
        "expires_at": float(expires_at),
        "state": "pending",
    }
    _write_payload(path, payload)
    return path


def clear_pending(directory: str | Path, hitl_id: str) -> None:
    """清除侧车；文件不存在不算错误。"""
    pending_path(directory, hitl_id).unlink(missing_ok=True)


def list_pending(directory: str | Path) -> list[dict[str, Any]]:
    """读取所有可解析的侧车；坏 JSON 记 warning 后跳过。"""
    root = pending_dir(directory)
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        logger.warning("failed to list push approval sidecars dir=%s", root)
        return []

    records: list[dict[str, Any]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("skip corrupt push approval sidecar path=%s", path)
            continue
        if not isinstance(payload, dict):
            logger.warning("skip non-object push approval sidecar path=%s", path)
            continue
        records.append(payload)
    return records


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _state_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _expired_card(*, hitl_id: str, run_id: str, target: str) -> ApprovalCard:
    return ApprovalCard(
        session_id=f"delivery:{run_id}",
        trace_id=hitl_id,
        agent_name="delivery_push_gate",
        intent=PUSH_APPROVAL_KIND,
        agent_output=(
            "⏰ 审批已失效，未推送；"
            f"需要继续请重新 delivery resume --run-id {run_id}"
        ),
        channel="feishu",
        target=target,
        hitl_kind="push_resolved_expired",
    )


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("failed to discard push approval sidecar path=%s", path)


async def sweep_push_cards(
    *,
    directory: str | Path,
    sender: Any | None,
    run_state_of: Callable[[str], Any],
    now: float,
    grace_s: float = 120,
    max_age_s: float = 7 * 24 * 3600,
) -> int:
    """把已过期的推送审批卡改成终态，返回成功改卡的数量。

    判据只有侧车时间与 run 状态；不读 Redis，也不要求 HITL 登记还在。
    """
    if sender is None:
        return 0

    now_value = float(now)
    swept = 0
    for record in list_pending(directory):
        if record.get("state") != "pending":
            continue
        hitl_id = str(record.get("hitl_id") or "")
        try:
            path = pending_path(directory, hitl_id)
        except ValueError:
            logger.warning(
                "discard push approval sidecar with invalid hitl_id=%r", hitl_id
            )
            continue

        created_at = _number(record.get("created_at"))
        expires_at = _number(record.get("expires_at"))
        message_id = str(record.get("card_message_id") or "")
        run_id = str(record.get("run_id") or "")
        target = str(record.get("target") or "")
        if not message_id:
            logger.warning(
                "discard push approval sidecar without message id hitl_id=%s",
                hitl_id,
            )
            _discard(path)
            continue
        if created_at is None or expires_at is None:
            logger.warning(
                "discard push approval sidecar with invalid timestamps hitl_id=%s",
                hitl_id,
            )
            _discard(path)
            continue

        aged_out = now_value >= created_at + float(max_age_s)
        expired = now_value >= expires_at + float(grace_s)
        abandoned = False
        if not expired:
            try:
                state = run_state_of(run_id)
            except Exception:  # noqa: BLE001 - 读 run 失败只退回按时间判定
                logger.warning(
                    "failed to read run state for push approval sweep run_id=%s",
                    run_id,
                    exc_info=True,
                )
                state = None
            if state is not None and _state_text(state) != _READY_STATE:
                abandoned = True

        if not (expired or abandoned or aged_out):
            continue

        updated = False
        try:
            updated = bool(
                await sender.update_card(
                    message_id,
                    _expired_card(hitl_id=hitl_id, run_id=run_id, target=target),
                )
            )
        except Exception:  # noqa: BLE001 - 改卡是展示层，失败保留侧车下一轮再试
            logger.warning(
                "failed to expire push approval card hitl_id=%s message_id=%s",
                hitl_id,
                message_id,
                exc_info=True,
            )

        if updated:
            swept += 1
            _discard(path)
            if aged_out:
                logger.warning(
                    "discard push approval sidecar after max age hitl_id=%s",
                    hitl_id,
                )
        elif aged_out:
            logger.warning(
                "discard push approval sidecar after max age hitl_id=%s", hitl_id
            )
            _discard(path)
    return swept
