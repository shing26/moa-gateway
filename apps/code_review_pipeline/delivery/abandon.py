"""人工终结交付（abandon）的单一写入口。

``abandon`` 不碰远端：它只在本地把运行记录收口成 ``abandoned``，并在 PR 已经
存在时先把对应的合并审批记录作废，避免出现"本地已放弃、卡片还能点"。两个
写入口必须按固定顺序执行——先 supersede 合并记录，成功后才写 ``ABANDONED``；
反过来会留下可点的卡片（契约 R4）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
from apps.code_review_pipeline.delivery.lockfile import (
    StaleLockReplaced,
    acquire,
    read_holder,
)
from apps.code_review_pipeline.delivery.models import (
    DeliveryRun,
    DeliveryRunState,
    FailureCode,
    FailureInfo,
)
from apps.code_review_pipeline.merge_state import MergeAction, MergeState

#: 只有这些状态允许人工终结。``merging`` / ``merged`` / ``rejected`` 明确拒绝：
#: 前两者可能已发出远端写，后者本身也是人工终态决定。
ABANDONABLE_STATES = frozenset(
    {
        DeliveryRunState.PREPARING,
        DeliveryRunState.IMPLEMENTING,
        DeliveryRunState.CHECKING,
        DeliveryRunState.VERIFIER_REVIEW,
        DeliveryRunState.REVISING,
        DeliveryRunState.READY_FOR_PUSH_APPROVAL,
        DeliveryRunState.PUSHING,
        DeliveryRunState.WAITING_CI,
        DeliveryRunState.AWAITING_MERGE_APPROVAL,
        DeliveryRunState.NEEDS_HUMAN,
        DeliveryRunState.FAILED,
        DeliveryRunState.BUDGET_EXHAUSTED,
    }
)

_REFUSED_STATES: dict[DeliveryRunState, str] = {
    DeliveryRunState.MERGING: (
        "merge may already have been sent to GitHub; abandoning the run "
        "cannot vouch for that in-flight call"
    ),
    DeliveryRunState.MERGED: (
        "the delivery already merged; merged is completion, not abandonment"
    ),
    DeliveryRunState.REJECTED: (
        "the delivery was already rejected by a human; repeating a terminal "
        "decision would only pollute the history"
    ),
}

_SUPERSEDABLE_MERGE_STATES = frozenset(
    {
        MergeState.WATCHING,
        MergeState.CI_FAILED,
        MergeState.AWAITING_APPROVAL,
    }
)

_ALREADY_TERMINAL_MERGE_STATES = frozenset(
    {
        MergeState.SUPERSEDED,
        MergeState.REJECTED,
        MergeState.FAILED,
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _lock_path(settings: Any, repo: str) -> Path:
    return (
        Path(settings.delivery_run_record_dir)
        / ".locks"
        / f"delivery-{repo.replace('/', '-')}.lock"
    )


@dataclass(frozen=True)
class AbandonOutcome:
    """abandon 的结论；exit_code 由 CLI 直接返回。"""

    exit_code: int
    run: DeliveryRun | None = None
    reason: str = ""
    merge_record: str = ""
    already: bool = False
    audit_failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        run = self.run
        return {
            "exit_code": self.exit_code,
            "run_id": run.run_id if run is not None else "",
            "state": run.state.value if run is not None else "",
            "failure": (
                run.failure.to_dict()
                if run is not None and run.failure is not None
                else None
            ),
            "pr": (
                run.push.pull_request.url
                if run is not None and run.push is not None
                else None
            ),
            "merge_record": self.merge_record,
            "already": self.already,
            "audit_failed": self.audit_failed,
            "reason": self.reason,
        }


def _refused(
    run: DeliveryRun | None,
    reason: str,
    *,
    merge_record: str = "",
    exit_code: int = 2,
) -> AbandonOutcome:
    return AbandonOutcome(
        exit_code=exit_code,
        run=run,
        reason=reason,
        merge_record=merge_record,
    )


async def abandon_run(
    run_id: str,
    *,
    store: Any,
    merge_store: Any,
    settings: Any,
    reason: str,
    operator: str = "",
    audit: Any = None,
) -> AbandonOutcome:
    """把一条运行记录终结为 ``abandoned``。

    ``merge_store`` 为 ``None`` 表示共享合并存储不可用；只有 ``run.push``
    存在时才因此拒绝。``audit`` 可注入替身，默认真实 WAL。
    """
    try:
        run = store.load(run_id)
    except FileNotFoundError:
        return _refused(None, f"delivery run not found: {run_id}")
    except ValueError as exc:
        return _refused(None, str(exc))

    if run.state is DeliveryRunState.ABANDONED:
        return AbandonOutcome(exit_code=0, run=run, already=True)

    if not reason.strip():
        return _refused(run, "reason is required; refusing to abandon")

    refused = _REFUSED_STATES.get(run.state)
    if refused is not None:
        return _refused(
            run,
            f"run state is {run.state.value}; {refused}",
            merge_record=(
                f"terminal:{run.state.value}"
                if run.state is not DeliveryRunState.MERGING
                else "merging"
            ),
        )
    if run.state not in ABANDONABLE_STATES:
        return _refused(
            run,
            f"run state is {run.state.value}; abandon does not accept it",
        )

    lock_path = _lock_path(settings, run.request.repo)
    lock = acquire(lock_path)
    if isinstance(lock, StaleLockReplaced):
        print(
            f"warning: replaced stale lock held by pid={lock.previous.pid} "
            f"started_at={lock.previous.started_at} host={lock.previous.host}",
            file=sys.stderr,
        )
    if lock is None:
        holder = read_holder(lock_path)
        if holder is not None:
            reason_text = (
                f"delivery 已在运行: pid={holder.pid} "
                f"started_at={holder.started_at} host={holder.host}; "
                f"拒绝在活进程下终结 ({lock_path})"
            )
        else:
            reason_text = f"delivery lock 被占用: {lock_path}"
        return _refused(run, reason_text, exit_code=3)

    try:
        merge_record = "none"
        if run.push is not None:
            if merge_store is None:
                return _refused(
                    run,
                    "delivery abandon 需要共享 MergeStore 来作废合并卡片；"
                    "请配置 VECTOR_DB_DSN 或 CODE_REVIEW_DATABASE_URL",
                )
            identity = (
                run.request.repo,
                run.push.pull_request.number,
                run.push.pull_request.head_sha,
            )
            try:
                record = merge_store.get(identity)
            except Exception as exc:  # noqa: BLE001 - 合并存储读失败必须拒绝整单
                return _refused(
                    run,
                    f"MergeStore unavailable: {type(exc).__name__}: {exc}",
                )
            if record is not None:
                try:
                    merge_state = MergeState(record.state)
                except ValueError:
                    return _refused(
                        run,
                        f"merge record has unknown state {record.state!r}; "
                        "refusing to abandon",
                    )
                if merge_state is MergeState.MERGED:
                    return _refused(
                        run,
                        "merge record is already merged; align the run with "
                        f"`delivery resume --run-id {run.run_id}` instead of "
                        "recording a merged PR as abandoned",
                        merge_record="terminal:merged",
                    )
                if merge_state is MergeState.MERGING:
                    return _refused(
                        run,
                        "merge record is merging; the remote write may already "
                        "have been sent, so abandon refuses",
                        merge_record="merging",
                    )
                if merge_state in _ALREADY_TERMINAL_MERGE_STATES:
                    merge_record = f"terminal:{merge_state.value}"
                elif merge_state in _SUPERSEDABLE_MERGE_STATES:
                    try:
                        merge_store.transition(
                            identity,
                            MergeAction.SUPERSEDE,
                            at=_utcnow().isoformat(),
                            failure_reason=reason,
                        )
                    except Exception as exc:  # noqa: BLE001 - supersede 失败即整单拒绝
                        return _refused(
                            run,
                            "failed to supersede the merge record: "
                            f"{type(exc).__name__}: {exc}",
                        )
                    merge_record = "superseded"
                else:
                    return _refused(
                        run,
                        f"merge record state {merge_state.value!r} cannot be "
                        "superseded; refusing to abandon",
                    )

        now = _utcnow()
        updated = replace(
            run,
            state=DeliveryRunState.ABANDONED,
            failure=FailureInfo(FailureCode.ABANDONED, reason),
            updated_at=now,
        )
        try:
            await store.save(updated)
        except OSError as exc:
            return _refused(
                run,
                f"failed to write the abandoned record: "
                f"{type(exc).__name__}: {exc}",
                merge_record=merge_record,
            )

        effective_audit = audit if audit is not None else WalDeliveryAudit()
        try:
            await effective_audit.record(
                run=updated,
                previous=run.state,
                current=DeliveryRunState.ABANDONED,
                detail={
                    "reason": reason,
                    "operator": operator,
                    "merge_record": merge_record,
                },
            )
        except Exception:  # noqa: BLE001 - 记录已终结；审计失败只报告、不回滚
            return AbandonOutcome(
                exit_code=1,
                run=updated,
                reason=(
                    "记录已终结、审计写入失败; audit must be replayed for "
                    f"run_id: {updated.run_id}"
                ),
                merge_record=merge_record,
                audit_failed=True,
            )
        return AbandonOutcome(
            exit_code=0,
            run=updated,
            merge_record=merge_record,
        )
    finally:
        lock.release()
