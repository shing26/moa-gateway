"""Delivery 断点重入：把搁浅的 DeliveryRun 重新带回它的下一站。

推送门禁长在 ``delivery run`` 的进程里；进程死了（会话重启、终端关闭、崩溃），
``ready_for_push_approval`` 的记录就搁浅了——管线没有回去的路。这里把恢复路径
收编成一等命令，只做三件事：按记录核对冻结证据、把人拉回同一个门禁前、走同一条
已审计的 publish。它绝不重跑实现者、绝不从工作区反推变更包——变更包只能来自
记录，工作区重新落地只是为了通过 publisher 自己的 ``_assert_bundle_current``。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agents.subprocess_env import harden_git_argv, isolated_env
from apps.code_review_pipeline.change.bundle import (
    ChangeBundle,
    build_change_bundle,
)
from apps.code_review_pipeline.change.pipeline import ChangePipeline
from apps.code_review_pipeline.delivery.adapters.github import GitHubChangeAdapter
from apps.code_review_pipeline.delivery.models import (
    DeliveryRun,
    DeliveryRunState,
    FailureCode,
    FailureInfo,
)
from apps.code_review_pipeline.delivery.ports import (
    BundleChanged,
    PushRecoveryRequired,
)
from apps.code_review_pipeline.delivery.state import assert_transition
from apps.code_review_pipeline.delivery.workspace import GitHubWorkspaceProvisioner


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ResumeOutcome:
    """resume 的结论：退出码由 CLI 直接返回，view 是 reconcile 分支的打印件。"""

    run: DeliveryRun
    exit_code: int
    refused: bool = False
    reason: str = ""
    view: dict[str, Any] | None = None


class ResumeRefused(RuntimeError):
    """记录或重新落地的工作区与冻结证据不一致；记录保持原样。"""


class _ApprovalDenied(Exception):
    """门禁未通过（拒绝或非 TTY）；记录必须留在 ready_for_push_approval。"""


# 门禁重入只认一个状态；其余要么能对齐合并通道，要么一律拒绝。
_GATE_REENTRY_STATE = DeliveryRunState.READY_FOR_PUSH_APPROVAL
_RECONCILE_STATES = frozenset(
    {
        DeliveryRunState.WAITING_CI,
        DeliveryRunState.AWAITING_MERGE_APPROVAL,
        DeliveryRunState.MERGING,
    }
)


def _git(args: list[str], *, cwd: Path) -> str:
    # 参数全部由本模块构造，不经 shell。
    return subprocess.run(
        harden_git_argv(args),
        cwd=cwd,
        env=isolated_env(cwd, git=True),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()


def _recorded_bundle_hash(run_id: str, record_dir: str | Path) -> str:
    """读记录 JSON 里落盘的 bundle_hash。

    ``store.load`` 不回读这个字段（它由 bundle 字段重算），而"重算哈希是否等于
    当初落盘的哈希"恰恰是记录完整性的判据，所以这里必须读原始文件。路径规则
    与 run store 相同：只认 alnum/-/_/.，其余 run_id 在这里就拒绝。
    """
    safe = "".join(
        char for char in run_id if char.isalnum() or char in {"-", "_", "."}
    )
    if not safe or safe != run_id:
        raise ResumeRefused(f"invalid run_id: {run_id!r}")
    path = Path(record_dir) / f"{run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    bundle = payload.get("bundle")
    if not isinstance(bundle, dict) or not bundle.get("bundle_hash"):
        raise ResumeRefused(f"record has no recorded bundle_hash: {path}")
    return str(bundle["bundle_hash"])


def _apply_recorded_patch(workspace: Path, bundle: ChangeBundle) -> None:
    """把记录里的 patch 原样打进重新落地的工作区。

    实现者写进工作区的是 LF 字节；core.autocrlf 若为 true，``git apply`` 写
    工作区时会把它们漂移成 CRLF，after_hash 随之改变——publisher 会正确拒绝。
    所以 repo 级 autocrlf 必须在 apply 之前钉死为 false。
    """
    patch_file = workspace.parent / "bundle.patch"
    patch_file.write_bytes(bundle.patch.encode("utf-8"))
    _git(["config", "core.autocrlf", "false"], cwd=workspace)
    _git(["apply", "--whitespace=nowarn", str(patch_file)], cwd=workspace)


async def resume_run(
    run_id: str,
    *,
    store: Any,
    github: Any,
    merge_store: Any,
    settings: Any,
    approval_port: Any,
) -> ResumeOutcome:
    """按记录状态分派：门禁重入 / 合并对齐 / 拒绝。"""
    run = store.load(run_id)
    if run.state in _RECONCILE_STATES:
        current = await store.reconcile(run, merge_store)
        return ResumeOutcome(run=current, exit_code=0, view=current.to_dict())
    if run.state is not _GATE_REENTRY_STATE:
        return ResumeOutcome(
            run=run,
            exit_code=2,
            refused=True,
            reason=(
                f"run state is {run.state.value}; resume re-enters only "
                "ready_for_push_approval (and reconciles waiting_ci / "
                "awaiting_merge_approval / merging)"
            ),
        )
    try:
        run, failure = await _reenter_push_gate(
            run,
            store=store,
            github=github,
            merge_store=merge_store,
            settings=settings,
            approval_port=approval_port,
        )
    except _ApprovalDenied:
        return ResumeOutcome(
            run=run,
            exit_code=1,
            reason="approval denied; nothing pushed",
        )
    except ResumeRefused as exc:
        return ResumeOutcome(run=run, exit_code=2, refused=True, reason=str(exc))
    except (subprocess.CalledProcessError, OSError) as exc:
        # 工作区重建阶段的本机 git/文件错误：发生在门禁之前，记录保持原样，
        # 修复环境后可以直接再跑。
        return ResumeOutcome(
            run=run,
            exit_code=2,
            refused=True,
            reason=f"workspace re-materialization failed: "
            f"{type(exc).__name__}: {exc}",
        )
    if failure is not None:
        return ResumeOutcome(run=run, exit_code=2, refused=True, reason=failure.message)
    return ResumeOutcome(run=run, exit_code=0)


async def _reenter_push_gate(
    run: DeliveryRun,
    *,
    store: Any,
    github: Any,
    merge_store: Any,
    settings: Any,
    approval_port: Any,
) -> tuple[DeliveryRun, FailureInfo | None]:
    """门禁重入主路径；failure 非 None 表示 publish 阶段已落终态。"""
    bundle = run.bundle
    if bundle is None:
        raise ResumeRefused("record has no bundle; nothing to re-approve")

    # 2a：证据核对——重算哈希必须等于记录里落盘的那个，否则这条记录已经被
    # 动过，恢复路径一个字节都不替它背书。
    recomputed = bundle.bundle_hash
    recorded = _recorded_bundle_hash(run.run_id, settings.delivery_run_record_dir)
    if recomputed != recorded:
        raise ResumeRefused(
            f"bundle_hash mismatch against the record: recomputed "
            f"{recomputed} != recorded {recorded}"
        )

    with tempfile.TemporaryDirectory(prefix="moa-delivery-resume-") as raw:
        # 2b：工作区按原流程重新落地（base_sha tarball + git baseline），再
        # 打上记录里的 patch；从工作区反推的包必须与记录逐字段一致。
        workspace = await GitHubWorkspaceProvisioner(github=github).materialize(
            repo=run.request.repo,
            base_sha=bundle.base_sha,
            destination=Path(raw) / "workspace",
        )
        _apply_recorded_patch(workspace, bundle)
        rebuilt = build_change_bundle(
            workspace=workspace,
            base_sha=bundle.base_sha,
            branch=bundle.branch,
            title=bundle.title,
            body=bundle.body,
            verify_command=bundle.verify_command,
            verify_exit_code=bundle.verify_exit_code,
            verify_output=bundle.verify_output,
            model=bundle.model,
            trace_id=bundle.trace_id,
            verify_output_hash=bundle.verify_output_hash,
        )
        if rebuilt.bundle_hash != recomputed:
            raise ResumeRefused(
                "re-materialized workspace does not reproduce the recorded "
                "bundle_hash"
            )

        # 2c：同一个终端门禁，操作者与 APPROVE 必须由人在 TTY 里敲。
        approval = await approval_port.request(run_id=run.run_id, bundle=bundle)
        if approval is None:
            raise _ApprovalDenied()

        # 2d：与 DeliveryFlow 同一条已审计 publish（内含 _assert_bundle_current
        # 兜底与合并登记），状态写入也按 _move 的形状落盘。
        assert_transition(run.state, DeliveryRunState.PUSHING)
        run = replace(run, state=DeliveryRunState.PUSHING, updated_at=_utcnow())
        await store.save(run)
        pipeline = ChangePipeline(
            github=github,
            repo=run.request.repo,
            workspace=workspace,
            verify_command=bundle.verify_command,
            model_name=bundle.model,
            base_branch=run.request.base_branch,
            push_enabled=True,
            verify_timeout_s=settings.change_agent_verify_timeout_s,
        )
        publisher = GitHubChangeAdapter(pipeline=pipeline, merge_store=merge_store)
        try:
            push = await publisher.publish(
                workspace=workspace,
                bundle=bundle,
                approval=approval,
            )
        except BundleChanged as exc:
            return (
                await _finish(
                    run,
                    DeliveryRunState.FAILED,
                    FailureInfo(FailureCode.BUNDLE_CHANGED, str(exc)),
                    store,
                ),
                FailureInfo(FailureCode.BUNDLE_CHANGED, str(exc)),
            )
        except PushRecoveryRequired as exc:
            saved = replace(run, push=exc.push, updated_at=_utcnow())
            await store.save(saved)
            failure = FailureInfo(FailureCode.INFRA_ERROR, str(exc))
            return (
                await _finish(
                    saved,
                    DeliveryRunState.NEEDS_HUMAN,
                    failure,
                    store,
                ),
                failure,
            )
        except Exception as exc:
            failure = FailureInfo(
                FailureCode.PUSH_FAILED,
                f"{type(exc).__name__}: {exc}",
            )
            return (
                await _finish(
                    run,
                    DeliveryRunState.FAILED,
                    failure,
                    store,
                ),
                failure,
            )

    assert_transition(run.state, DeliveryRunState.WAITING_CI)
    run = replace(
        run,
        state=DeliveryRunState.WAITING_CI,
        push=push,
        updated_at=_utcnow(),
    )
    await store.save(run)
    return run, None


async def _finish(
    run: DeliveryRun,
    target: DeliveryRunState,
    failure: FailureInfo,
    store: Any,
) -> DeliveryRun:
    assert_transition(run.state, target)
    updated = replace(
        run,
        state=target,
        failure=failure,
        updated_at=_utcnow(),
    )
    await store.save(updated)
    return updated
