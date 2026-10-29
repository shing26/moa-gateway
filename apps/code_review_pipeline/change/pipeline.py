"""变更管线（ADR-030）：验证 → 审批 → 推送，顺序固定，dry-run 默认。

这条管线只做一件事：把 agent 在工作区里的改动变成**可复核的变更包**，并在
满足验证与审批条件后（且显式开启推送时）才写入远端。默认路径不产生任何
远端副作用。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from app.agents.command_runner import run_command
from app.agents.workspace_tools import create_verification_workspace
from apps.code_review_pipeline.change.bundle import (
    ChangeBundle,
    build_change_bundle,
    verification_output_excerpt,
    verification_output_hash,
)
from apps.code_review_pipeline.change.refs import PullRequestRef

logger = logging.getLogger("moa.change")


class ChangeStatus(str, Enum):
    VERIFIED = "verified"  # 验证通过，产出可推送的包
    VERIFY_FAILED = "verify_failed"
    NO_CHANGES = "no_changes"
    NO_VERIFY_COMMAND = "no_verify_command"
    MODEL_NOT_CONFIGURED = "model_not_configured"
    INVALID_BRANCH = "invalid_branch"
    NOT_APPROVED = "not_approved"
    PUSH_DISABLED = "push_disabled"
    PUSHED = "pushed"


class ChangeBundleChanged(RuntimeError):
    """The workspace no longer matches the bundle a human approved."""


@dataclass(frozen=True)
class ChangeRequest:
    repo: str
    base_sha: str
    title: str
    body: str
    goal: str
    branch: str
    trace_id: str = ""


@dataclass(frozen=True)
class ChangeApproval:
    approver: str
    trace_id: str
    bundle_hash: str
    decision: str = "approve"
    run_id: str = ""


@dataclass(frozen=True)
class ChangeResult:
    status: ChangeStatus
    bundle: ChangeBundle | None = None
    message: str = ""
    pr_url: str = ""
    commit_sha: str = ""
    pull_request: PullRequestRef | None = None
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {ChangeStatus.VERIFIED, ChangeStatus.PUSHED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "pr_url": self.pr_url,
            "commit_sha": self.commit_sha,
            "pull_request": (
                {
                    "number": self.pull_request.number,
                    "url": self.pull_request.url,
                    "head_sha": self.pull_request.head_sha,
                }
                if self.pull_request
                else None
            ),
            "bundle": self.bundle.to_dict() if self.bundle else None,
            "audit": dict(self.audit),
        }


class ChangeAgent(Protocol):
    async def run(self, *, workspace: Path, goal: str) -> str: ...


class ApprovalRecorder(Protocol):
    async def __call__(
        self,
        *,
        task_id: str,
        repo: str,
        operator: str,
        decision: str,
        run_id: str = "",
        trace_id: str = "",
        bundle_hash: str = "",
    ) -> None: ...


class ChangePipeline:
    """把 agent 改动变成变更包；推送需要显式开关 + 审批记录。"""

    def __init__(
        self,
        *,
        github: Any,
        repo: str,
        workspace: Path,
        verify_command: tuple[str, ...],
        model_name: str,
        base_branch: str = "main",
        push_enabled: bool = False,
        approval_recorder: ApprovalRecorder | None = None,
    ) -> None:
        self._github = github
        self._repo = repo
        self._workspace = Path(workspace).resolve()
        self._verify_command = tuple(verify_command)
        self._model_name = model_name.strip()
        self._base_branch = base_branch.strip() or "main"
        self._push_enabled = bool(push_enabled)
        self._approval_recorder = approval_recorder

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def repo(self) -> str:
        return self._repo

    @property
    def verify_command(self) -> tuple[str, ...]:
        return self._verify_command

    async def run(
        self,
        request: ChangeRequest,
        *,
        agent: ChangeAgent | None = None,
        approval: ChangeApproval | None = None,
    ) -> ChangeResult:
        rejected = self._preflight(request)
        if rejected is not None:
            return rejected
        if agent is not None:
            await agent.run(workspace=self._workspace, goal=request.goal)
        prepared = await self.prepare_bundle(request)
        if prepared.status is not ChangeStatus.VERIFIED:
            return prepared
        assert prepared.bundle is not None
        return await self.publish_bundle(prepared.bundle, approval)

    async def prepare_bundle(self, request: ChangeRequest) -> ChangeResult:
        rejected = self._preflight(request)
        if rejected is not None:
            return rejected

        verify = await self._verify()
        output_hash = verification_output_hash(verify.stdout, verify.stderr)
        branch = _traceable_branch(request.branch, request.trace_id)
        bundle = build_change_bundle(
            workspace=self._workspace,
            base_sha=request.base_sha,
            branch=branch,
            title=request.title,
            body=request.body,
            verify_command=self._verify_command,
            verify_exit_code=verify.returncode,
            verify_output=verification_output_excerpt(verify.stdout, verify.stderr),
            verify_output_hash=output_hash,
            model=self._model_name,
            trace_id=request.trace_id,
        )
        audit = {
            "repo": self._repo,
            "base_sha": request.base_sha,
            "bundle_hash": bundle.bundle_hash,
            "patch_hash": bundle.patch_hash,
            "verify_exit_code": verify.returncode,
            "approver": "",
            "push_enabled": self._push_enabled,
        }

        if verify.returncode != 0:
            return ChangeResult(
                status=ChangeStatus.VERIFY_FAILED,
                bundle=bundle,
                message=f"验证失败（exit={verify.returncode}），不生成可推送的包",
                audit=audit,
            )
        if not bundle.files:
            return ChangeResult(
                status=ChangeStatus.NO_CHANGES,
                bundle=bundle,
                message="工作区没有改动",
                audit=audit,
            )
        return ChangeResult(
            status=ChangeStatus.VERIFIED,
            bundle=bundle,
            message="验证通过，等待人工推送审批",
            audit=audit,
        )

    def _preflight(self, request: ChangeRequest) -> ChangeResult | None:
        if request.repo.strip() != self._repo:
            raise ValueError(
                f"request repo does not match pipeline repo: "
                f"{request.repo!r} != {self._repo!r}"
            )
        if not self._verify_command:
            return ChangeResult(
                status=ChangeStatus.NO_VERIFY_COMMAND,
                message="未配置验证命令；未验证的变更不允许出口（ADR-030 决策 4）",
            )
        if not self._model_name:
            return ChangeResult(
                status=ChangeStatus.MODEL_NOT_CONFIGURED,
                message="未配置变更模型；拒绝静默降级成规则拼装（ADR-030 决策 8）",
            )
        return None

    async def publish_bundle(
        self,
        bundle: ChangeBundle,
        approval: ChangeApproval | None,
    ) -> ChangeResult:
        audit = {
            "repo": self._repo,
            "base_sha": bundle.base_sha,
            "bundle_hash": bundle.bundle_hash,
            "patch_hash": bundle.patch_hash,
            "verify_exit_code": bundle.verify_exit_code,
            "approver": approval.approver if approval is not None else "",
            "push_enabled": self._push_enabled,
        }
        if bundle.branch.strip() == self._base_branch:
            return ChangeResult(
                status=ChangeStatus.INVALID_BRANCH,
                bundle=bundle,
                message="refusing to write the configured base branch",
                audit=audit,
            )
        if not bundle.is_verified:
            return ChangeResult(
                status=ChangeStatus.VERIFY_FAILED,
                bundle=bundle,
                message="bundle is not verified or contains no changes",
                audit=audit,
            )
        if not self._push_enabled:
            return ChangeResult(
                status=ChangeStatus.PUSH_DISABLED,
                bundle=bundle,
                message="dry-run：变更包已生成，未写入远端",
                audit=audit,
            )
        if not bundle.trace_id.strip():
            raise ValueError("trace_id is required for a traceable push")
        approval_granted = approval is not None and (
            approval.decision == "approve" and bool(approval.approver.strip())
        )
        if approval is not None and not approval.approver.strip():
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="approval record requires a non-empty approver",
                audit=audit,
            )
        if approval is not None and approval.trace_id != bundle.trace_id:
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="approval record does not match the current trace_id",
                audit=audit,
            )
        if approval is not None and approval.bundle_hash != bundle.bundle_hash:
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="approval record does not match the current bundle hash",
                audit=audit,
            )
        if not approval_granted:
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="推送需要人工审批（ADR-030 决策 3）",
                audit=audit,
            )

        assert approval is not None
        self._assert_bundle_current(bundle)
        await self._require_remote_base(bundle.base_sha)
        await self._record_approval(bundle, approval)
        commit_sha, pull_request = await self._push(bundle)
        return ChangeResult(
            status=ChangeStatus.PUSHED,
            bundle=bundle,
            message="已推送并创建 PR",
            pr_url=pull_request.url,
            commit_sha=commit_sha,
            pull_request=pull_request,
            audit=audit,
        )

    def _assert_bundle_current(self, bundle: ChangeBundle) -> None:
        current = build_change_bundle(
            workspace=self._workspace,
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
        if current.bundle_hash != bundle.bundle_hash:
            raise ChangeBundleChanged("workspace changed after bundle")

    async def _verify(self) -> subprocess.CompletedProcess[str]:
        verification_root, temporary_parent = await asyncio.to_thread(
            create_verification_workspace, self._workspace
        )
        try:
            result = await asyncio.to_thread(
                run_command,
                self._verify_command,
                cwd=verification_root,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, temporary_parent, True)
        return subprocess.CompletedProcess(
            args=list(self._verify_command),
            returncode=result.exit_code,
            stdout=result.output,
            stderr="",
        )

    async def _record_approval(
        self,
        bundle: ChangeBundle,
        approval: ChangeApproval,
    ) -> None:
        recorder = self._approval_recorder
        if recorder is None:
            from apps.code_review_pipeline.task_audit import record_human_decision

            recorder = record_human_decision
        arguments = {
            "task_id": bundle.trace_id,
            "repo": self._repo,
            "operator": approval.approver.strip(),
            "decision": f"approve_change:{bundle.bundle_hash}",
        }
        metadata = {
            "run_id": approval.run_id,
            "trace_id": bundle.trace_id,
            "bundle_hash": bundle.bundle_hash,
        }
        try:
            signature = inspect.signature(recorder)
        except (TypeError, ValueError):
            accepts_metadata = False
        else:
            accepts_metadata = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            ) or all(name in signature.parameters for name in metadata)
        if accepts_metadata:
            await recorder(**arguments, **metadata)
        else:
            await recorder(**arguments)

    async def _push(self, bundle: ChangeBundle) -> tuple[str, PullRequestRef]:
        """只做三件事：建分支、建 commit、开 PR。不删分支、不 force push。"""
        files = self._file_payload(bundle)
        create_ref = getattr(self._github, "create_ref", None)
        create_commit = getattr(self._github, "create_commit", None)
        create_pr = getattr(self._github, "create_pull_request", None)
        if not all((create_ref, create_commit, create_pr)):
            raise NotImplementedError(
                "GitHub 客户端缺少写侧方法；dry-run 之外的操作尚不可用"
            )
        repo = _split_repo(self._repo)
        await self._require_remote_base(bundle.base_sha)
        await create_ref(repo, bundle.branch, bundle.base_sha)
        commit_sha = str(
            await create_commit(
                repo,
                base_sha=bundle.base_sha,
                branch=bundle.branch,
                message=(
                    f"{bundle.title}\n\n{bundle.body}\n\n"
                    f"MoA-Trace-Id: {bundle.trace_id}\n"
                    f"MoA-Bundle-Hash: {bundle.bundle_hash}"
                ).strip(),
                files=files,
            )
        )
        pull_request = await create_pr(
            repo,
            branch=bundle.branch,
            base_branch=self._base_branch,
            title=bundle.title,
            body=bundle.body,
        )
        if not isinstance(pull_request, PullRequestRef):
            raise TypeError("create_pull_request must return PullRequestRef")
        if pull_request.head_sha != commit_sha:
            raise RuntimeError(
                "GitHub PR head SHA does not match the created commit: "
                f"{pull_request.head_sha!r} != {commit_sha!r}"
            )
        return commit_sha, pull_request

    async def _remote_has_commit(self, repo: Any, sha: str) -> bool:
        probe = getattr(self._github, "commit_exists", None)
        if probe is None:
            raise RuntimeError(
                "GitHub client does not implement commit_exists; refusing to push"
            )
        return bool(await probe(repo, sha))

    async def _require_remote_base(self, base_sha: str) -> None:
        # 本地临时 HEAD 不是远端 commit 时，GitHub 建 ref 只会回一个 422，
        # 让人误以为是权限或分支问题；在审批审计前就把原因说清楚。
        repo = _split_repo(self._repo)
        if not await self._remote_has_commit(repo, base_sha):
            raise ValueError(
                f"base_sha 不存在于远端仓库 {self._repo}: {base_sha[:12]}…；"
                "变更必须基于远端 commit，而不是本地临时 HEAD"
            )

    def _file_payload(self, bundle: ChangeBundle) -> list[dict[str, Any]]:
        """把变更包里的路径读成 Git Data API 需要的 base64 内容。"""
        payload: list[dict[str, Any]] = []
        for change in bundle.files:
            target = self._workspace / change.path
            parent = target.parent.resolve()
            if parent != self._workspace and not parent.is_relative_to(
                self._workspace
            ):
                raise ValueError(f"变更路径越界: {change.path}")
            if change.kind != "deleted" and target.is_symlink() != (
                change.mode == "120000"
            ):
                raise ChangeBundleChanged(
                    f"file mode changed after bundle: {change.path}"
                )
            if change.kind == "deleted":
                if target.exists() or target.is_symlink():
                    raise ChangeBundleChanged(
                        f"file changed after bundle: {change.path} was recreated"
                    )
                payload.append(
                    {
                        "path": change.path,
                        "content_b64": None,
                        "mode": change.mode,
                    }
                )
                continue
            if change.mode == "120000":
                content = str(os.readlink(target)).encode(
                    "utf-8",
                    errors="surrogateescape",
                )
            else:
                target = target.resolve()
                if not target.is_relative_to(self._workspace):
                    raise ValueError(f"变更路径越界: {change.path}")
                try:
                    content = target.read_bytes()
                except FileNotFoundError as exc:
                    raise ChangeBundleChanged(
                        f"file changed after bundle: {change.path}"
                    ) from exc
            digest = hashlib.sha256(content).hexdigest()
            if digest != change.after_hash:
                raise ChangeBundleChanged(
                    f"file changed after bundle: {change.path}"
                )
            payload.append(
                {
                    "path": change.path,
                    "content_b64": base64.b64encode(content).decode("ascii"),
                    "mode": change.mode,
                }
            )
        return payload


def _split_repo(repo: str) -> Any:
    from apps.code_review_pipeline.routing.github_client import GitHubRepo

    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"repo 必须是 owner/name 形式: {repo!r}")
    return GitHubRepo(owner=owner, name=name)


def _traceable_branch(branch: str, trace_id: str) -> str:
    base = branch.strip().strip("/")
    suffix = re.sub(r"[^A-Za-z0-9._-]+", "-", trace_id.strip()).strip("-")
    if not suffix or base.endswith(f"-{suffix}"):
        return base
    return f"{base}-{suffix}"


def verify_command_from_env(raw: str) -> tuple[str, ...]:
    """解析部署方提供的验证命令；空值表示未配置。"""
    text = (raw or "").strip()
    return tuple(shlex.split(text)) if text else ()


__all__ = [
    "ChangeAgent",
    "ChangeApproval",
    "ChangeBundleChanged",
    "ChangePipeline",
    "ChangeRequest",
    "ChangeResult",
    "ChangeStatus",
    "ApprovalRecorder",
    "verify_command_from_env",
]
