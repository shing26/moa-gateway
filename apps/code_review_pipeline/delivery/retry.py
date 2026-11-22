"""Delivery 失败续跑：把 FAILED / BUDGET_EXHAUSTED 变成一单新的完整交付。

``delivery resume`` 原来只认两条路：推送门禁重入与合并状态对齐；失败记录
只能整单重来。这里给失败一个一等出路：命中可续跑失败码时，从父记录复制
请求、换全新 run_id / trace_id、预算从 settings 重新取满，走与
``delivery run`` 完全相同的 DeliveryFlow（implementer → 验证 → 推送门禁 →
publish）。新单照常过人审，父记录一个字节不动。

fail-closed 是底色：PUSH_FAILED（可能已部分推送、PR 可能已存在）、
CI / MERGE 类失败（已有 PR，走 ci-triage）、APPROVAL_REJECTED（人的决定，
自动重跑等于绕过人审）一律拒绝自动续跑。
"""

from __future__ import annotations

import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from apps.code_review_pipeline.delivery.lockfile import (
    acquire,
    read_holder,
    StaleLockReplaced,
)
from apps.code_review_pipeline.delivery.models import (
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    FailureCode,
)
from apps.code_review_pipeline.delivery.repo_profile import (
    RepoProfileError,
    resolve_delivery_config,
)

FlowFactory = Callable[..., Any]

#: 父记录的失败码命中其一才允许续跑；BUDGET_EXHAUSTED 态恒带 INFRA_ERROR。
RESUMABLE_FAILURE_CODES = frozenset(
    {
        FailureCode.NO_CHANGES,
        FailureCode.VERIFY_FAILED,
        FailureCode.VERIFIER_INVALID,
        FailureCode.VERIFIER_REVISE_EXHAUSTED,
        FailureCode.PREFLIGHT_FAILED,
        FailureCode.BUNDLE_CHANGED,
        FailureCode.INFRA_ERROR,
    }
)

#: 只有这两个终态走"新开一单"分支；其余状态维持 resume 既有分派不变。
RETRY_STATES = frozenset(
    {DeliveryRunState.FAILED, DeliveryRunState.BUDGET_EXHAUSTED}
)

#: fail-closed 拒绝面：每个码写清"为什么不能自动重跑"。
_REFUSAL_REASONS = {
    FailureCode.PUSH_FAILED: (
        "push may have partially succeeded (a PR may already exist); "
        "human triage required before any rerun"
    ),
    FailureCode.CI_FAILED: (
        "a PR already exists; use delivery ci-triage, not a rerun"
    ),
    FailureCode.MERGE_SHA_MISMATCH: (
        "a PR already exists; use delivery ci-triage, not a rerun"
    ),
    FailureCode.MERGE_NOT_MERGEABLE: (
        "a PR already exists; use delivery ci-triage, not a rerun"
    ),
    FailureCode.MERGE_FAILED: (
        "a PR already exists; use delivery ci-triage, not a rerun"
    ),
    FailureCode.APPROVAL_REJECTED: (
        "approval was a human decision; auto-rerun would bypass review"
    ),
}


@dataclass(frozen=True)
class RetryOutcome:
    """retry 的结论：exit_code 由 CLI 直接返回；run 为 None 表示没有新单。"""

    parent_run_id: str
    exit_code: int
    run: DeliveryRun | None = None
    refused: bool = False
    reason: str = ""


class RetryBlocked(RuntimeError):
    """执行环境不满足（共享 MergeStore 缺失等）；不创建新 run。"""


def _refused(run_id: str, reason: str, *, exit_code: int = 2) -> RetryOutcome:
    return RetryOutcome(
        parent_run_id=run_id,
        exit_code=exit_code,
        refused=True,
        reason=reason,
    )


def _profile_root(settings: Any) -> Path:
    """仓库档案根：与 app/cli.py 的 _delivery_repo_profile_root 同一规则。"""
    configured = Path(
        getattr(settings, "delivery_repo_profile_dir", "config/repos")
    )
    if configured.is_absolute():
        return configured
    return Path(__file__).resolve().parents[3] / configured


def _exit_code_for(run: DeliveryRun) -> int:
    return (
        0
        if run.state
        in {DeliveryRunState.WAITING_CI, DeliveryRunState.READY_FOR_PUSH_APPROVAL}
        else 1
    )


async def retry_failed_run(
    run_id: str,
    *,
    store: Any,
    settings: Any,
    flow_factory: FlowFactory | None = None,
    profile_root: Path | None = None,
) -> RetryOutcome:
    """按父记录判定可否续跑；可以就新开一单走完整 DeliveryFlow。

    ``flow_factory`` 是测试注入点：传入时以 ``request`` / ``policy`` /
    ``store`` 关键字调用并 ``await flow.run(request)``；默认 None 时内部
    自建与 ``delivery run`` 同构的真实执行栈。
    """
    parent = store.load(run_id)
    if parent.state not in RETRY_STATES:
        return _refused(
            run_id,
            f"run state is {parent.state.value}; retry accepts only "
            "failed / budget_exhausted records",
        )
    code = parent.failure.code if parent.failure is not None else None
    if code not in RESUMABLE_FAILURE_CODES:
        reason = _REFUSAL_REASONS.get(code) if code is not None else None
        if reason is None:
            reason = (
                f"failure code is not retryable: {code.value}"
                if code is not None
                else "no failure recorded; refusing to auto-rerun"
            )
        return _refused(run_id, reason)

    root = profile_root if profile_root is not None else _profile_root(settings)
    try:
        resolved = resolve_delivery_config(
            parent.request.repo,
            root,
            env_verify_command=settings.change_agent_verify_command,
            env_base_branch=settings.change_agent_base_branch,
        )
    except RepoProfileError as exc:
        return _refused(run_id, f"仓库档案不可用: {exc}")
    verify_command = resolved.verify_command
    if not verify_command:
        return _refused(run_id, "CHANGE_AGENT_VERIFY_COMMAND 未配置")
    if settings.delivery_topology != "single":
        return _refused(
            run_id,
            "delivery retry 当前只支持 single topology；verified topology "
            "属于另行规划的 S1 实验，不能静默降级。",
        )
    if not settings.change_agent_model.strip():
        return _refused(run_id, "CHANGE_AGENT_MODEL 未配置")

    # 预算与模型从 settings 取当前值——"预算用尽重跑"的意义就在于预算重置。
    request = replace(
        parent.request,
        trace_id=f"delivery-{time.time_ns()}",
        source_ref=f"retry:{run_id}",
        base_branch=resolved.base_branch,
    )
    policy = DeliveryPolicy(
        verify_command=verify_command,
        topology=settings.delivery_topology,
        max_steps=settings.delivery_max_steps,
        max_tokens=settings.delivery_max_tokens,
    )

    # 与 delivery run 同一把 (record_dir, repo) 锁：同仓库同时只许一个
    # delivery 进程，续跑也不例外。
    lock_path = (
        Path(settings.delivery_run_record_dir)
        / ".locks"
        / f"delivery-{request.repo.replace('/', '-')}.lock"
    )
    lock = acquire(lock_path)
    if isinstance(lock, StaleLockReplaced):
        print(
            f"warning: replaced stale lock held by pid={lock.previous.pid} started_at={lock.previous.started_at} host={lock.previous.host}",
            file=sys.stderr,
        )
    if lock is None:
        holder = read_holder(lock_path)
        if holder is not None:
            reason = (
                f"delivery 已在运行: pid={holder.pid} "
                f"started_at={holder.started_at} host={holder.host}; "
                f"拒绝重复启动 ({lock_path})"
            )
        else:
            reason = f"delivery lock 被占用: {lock_path}"
        return _refused(run_id, reason, exit_code=3)
    try:
        if flow_factory is not None:
            flow = flow_factory(request=request, policy=policy, store=store)
            run = await flow.run(request)
        else:
            run = await _run_real_flow(
                request,
                policy,
                settings=settings,
                fast_verify_command=resolved.fast_verify_command,
            )
    except RetryBlocked as exc:
        return _refused(run_id, str(exc))
    finally:
        lock.release()
    return RetryOutcome(
        parent_run_id=run_id,
        exit_code=_exit_code_for(run),
        run=run,
    )


async def _run_real_flow(
    request: DeliveryRequest,
    policy: DeliveryPolicy,
    *,
    settings: Any,
    fast_verify_command: tuple[str, ...] | None = None,
) -> DeliveryRun:
    """与 cmd_delivery_run 同构的真实执行栈；宁可重复也不动黄金路径。"""
    from app.agents.provider import LLMClient, LLMConfig
    from app.cli import _build_push_approval_port
    from apps.code_review_pipeline.change.pipeline import ChangePipeline
    from apps.code_review_pipeline.delivery.adapters.github import (
        GitHubChangeAdapter,
    )
    from apps.code_review_pipeline.delivery.adapters.production_agent import (
        ProductionImplementer,
    )
    from apps.code_review_pipeline.delivery.audit import WalDeliveryAudit
    from apps.code_review_pipeline.delivery.flow import DeliveryFlow
    from apps.code_review_pipeline.delivery.run_store import DeliveryRunStore
    from apps.code_review_pipeline.delivery.workspace import (
        GitHubWorkspaceProvisioner,
    )
    from apps.code_review_pipeline.merge_store import (
        PostgresMergeStore,
        build_merge_store,
    )
    from apps.code_review_pipeline.routing.github_provider import (
        build_github_client,
    )

    merge_store = build_merge_store()
    if not isinstance(merge_store, PostgresMergeStore):
        close = getattr(merge_store, "close", None)
        if close is not None:
            await close()
        raise RetryBlocked(
            "delivery retry 需要共享 MergeStore；请配置 VECTOR_DB_DSN 或 "
            "CODE_REVIEW_DATABASE_URL"
        )

    config = LLMConfig.from_env("LLM")
    config.model = settings.change_agent_model
    llm = LLMClient(config)
    github = build_github_client()

    async def execute() -> DeliveryRun:
        try:
            with tempfile.TemporaryDirectory(prefix="moa-delivery-retry-") as raw:
                workspace = await GitHubWorkspaceProvisioner(
                    github=github
                ).materialize(
                    repo=request.repo,
                    base_sha=request.base_sha,
                    destination=Path(raw) / "workspace",
                )
                pipeline = ChangePipeline(
                    github=github,
                    repo=request.repo,
                    workspace=workspace,
                    verify_command=policy.verify_command,
                    model_name=settings.change_agent_model,
                    base_branch=request.base_branch,
                    push_enabled=True,
                    verify_timeout_s=settings.change_agent_verify_timeout_s,
                )
                adapter = GitHubChangeAdapter(
                    pipeline=pipeline,
                    merge_store=merge_store,
                )
                flow = DeliveryFlow(
                    workspace=workspace,
                    policy=policy,
                    implementer=ProductionImplementer(
                        llm=llm,
                        verify_command=fast_verify_command or policy.verify_command,
                        command_timeout_s=settings.change_agent_verify_timeout_s,
                    ),
                    preparer=adapter,
                    approval=_build_push_approval_port(github),
                    publisher=adapter,
                    recorder=DeliveryRunStore(
                        settings.delivery_run_record_dir
                    ),
                    audit=WalDeliveryAudit(),
                    github=github,
                    heartbeat_dir=Path(settings.delivery_run_record_dir),
                )
                return await flow.run(request)
        finally:
            await merge_store.close()
            await llm.aclose()
            await github.aclose()

    return await execute()
