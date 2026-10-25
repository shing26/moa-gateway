"""变更管线（ADR-030）：验证 → 审批 → 推送，顺序固定，dry-run 默认。

这条管线只做一件事：把 agent 在工作区里的改动变成**可复核的变更包**，并在
满足验证与审批条件后（且显式开启推送时）才写入远端。默认路径不产生任何
远端副作用。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shlex
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from apps.code_review_pipeline.change.bundle import ChangeBundle, build_change_bundle

logger = logging.getLogger("moa.change")


class ChangeStatus(str, Enum):
    VERIFIED = "verified"  # 验证通过，产出可推送的包
    VERIFY_FAILED = "verify_failed"
    NO_CHANGES = "no_changes"
    NO_VERIFY_COMMAND = "no_verify_command"
    MODEL_NOT_CONFIGURED = "model_not_configured"
    NOT_APPROVED = "not_approved"
    PUSH_DISABLED = "push_disabled"
    PUSHED = "pushed"


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
class ChangeResult:
    status: ChangeStatus
    bundle: ChangeBundle | None = None
    message: str = ""
    pr_url: str = ""
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {ChangeStatus.VERIFIED, ChangeStatus.PUSHED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "pr_url": self.pr_url,
            "bundle": self.bundle.to_dict() if self.bundle else None,
            "audit": dict(self.audit),
        }


class ChangeAgent(Protocol):
    async def run(self, *, workspace: Path, goal: str) -> str: ...


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
    ) -> None:
        self._github = github
        self._repo = repo
        self._workspace = Path(workspace).resolve()
        self._verify_command = tuple(verify_command)
        self._model_name = model_name.strip()
        self._base_branch = base_branch.strip() or "main"
        self._push_enabled = bool(push_enabled)

    async def run(
        self,
        request: ChangeRequest,
        *,
        agent: ChangeAgent | None = None,
        approved: bool = False,
        approver: str = "",
    ) -> ChangeResult:
        """执行一次变更。agent 为 None 时视为"工作区已由外部改好"。"""
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

        if agent is not None:
            await agent.run(workspace=self._workspace, goal=request.goal)

        verify = await self._verify()
        bundle = build_change_bundle(
            workspace=self._workspace,
            base_sha=request.base_sha,
            branch=request.branch,
            title=request.title,
            body=request.body,
            verify_command=self._verify_command,
            verify_exit_code=verify.returncode,
            verify_output=verify.stdout[-4000:],
            model=self._model_name,
            trace_id=request.trace_id,
        )
        audit = {
            "repo": request.repo,
            "base_sha": request.base_sha,
            "bundle_hash": bundle.bundle_hash,
            "patch_hash": bundle.patch_hash,
            "verify_exit_code": verify.returncode,
            "approver": approver,
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

        if not self._push_enabled:
            return ChangeResult(
                status=ChangeStatus.PUSH_DISABLED,
                bundle=bundle,
                message="dry-run：变更包已生成，未写入远端",
                audit=audit,
            )
        if not approved:
            return ChangeResult(
                status=ChangeStatus.NOT_APPROVED,
                bundle=bundle,
                message="推送需要人工审批（ADR-030 决策 3）",
                audit=audit,
            )

        pr_url = await self._push(bundle)
        return ChangeResult(
            status=ChangeStatus.PUSHED,
            bundle=bundle,
            message="已推送并创建 PR",
            pr_url=pr_url,
            audit=audit,
        )

    async def _verify(self) -> subprocess.CompletedProcess[str]:
        argv = [*self._verify_command]
        return await asyncio.to_thread(
            subprocess.run,  # noqa: S603 - 命令来自部署方配置
            argv,
            cwd=str(self._workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    async def _push(self, bundle: ChangeBundle) -> str:
        """只做三件事：建分支、建 commit、开 PR。不删分支、不 force push。"""
        create_ref = getattr(self._github, "create_ref", None)
        create_commit = getattr(self._github, "create_commit", None)
        create_pr = getattr(self._github, "create_pull_request", None)
        if not all((create_ref, create_commit, create_pr)):
            raise NotImplementedError(
                "GitHub 客户端缺少写侧方法；dry-run 之外的操作尚不可用"
            )
        repo = _split_repo(self._repo)
        # base_sha 必须是远端真实 commit。本地工作区的 HEAD 可能是临时克隆的
        # 提交，GitHub 建 ref 时只会回一个 422，让人误以为是权限或分支问题。
        if not await self._remote_has_commit(repo, bundle.base_sha):
            raise ValueError(
                f"base_sha 不存在于远端仓库 {self._repo}: {bundle.base_sha[:12]}…；"
                "变更必须基于远端 commit，而不是本地临时 HEAD"
            )
        await create_ref(repo, bundle.branch, bundle.base_sha)
        files = self._file_payload(bundle)
        await create_commit(
            repo,
            base_sha=bundle.base_sha,
            branch=bundle.branch,
            message=f"{bundle.title}\n\n{bundle.body}".strip(),
            files=files,
        )
        return await create_pr(
            repo,
            branch=bundle.branch,
            base_branch=self._base_branch,
            title=bundle.title,
            body=bundle.body,
        )

    async def _remote_has_commit(self, repo: Any, sha: str) -> bool:
        probe = getattr(self._github, "commit_exists", None)
        if probe is None:
            return True  # 客户端不支持探测时不阻断，交给远端校验
        return bool(await probe(repo, sha))

    def _file_payload(self, bundle: ChangeBundle) -> list[dict[str, str]]:
        """把变更包里的路径读成 Git Data API 需要的 base64 内容。"""
        payload: list[dict[str, str]] = []
        for change in bundle.files:
            target = (self._workspace / change.path).resolve()
            if not target.is_relative_to(self._workspace):
                raise ValueError(f"变更路径越界: {change.path}")
            if change.kind == "deleted":
                payload.append({"path": change.path, "content_b64": None})
                continue
            payload.append(
                {
                    "path": change.path,
                    "content_b64": base64.b64encode(target.read_bytes()).decode("ascii"),
                }
            )
        return payload


def _split_repo(repo: str) -> Any:
    from apps.code_review_pipeline.routing.github_client import GitHubRepo

    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"repo 必须是 owner/name 形式: {repo!r}")
    return GitHubRepo(owner=owner, name=name)


def verify_command_from_env(raw: str) -> tuple[str, ...]:
    """解析部署方提供的验证命令；空值表示未配置。"""
    text = (raw or "").strip()
    return tuple(shlex.split(text)) if text else ()


__all__ = [
    "ChangeAgent",
    "ChangePipeline",
    "ChangeRequest",
    "ChangeResult",
    "ChangeStatus",
    "verify_command_from_env",
]
