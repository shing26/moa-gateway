"""交付开工前的能力自检：把最贵的失败提前到最便宜的时刻。

两次真实交付（``dlv-136442b2``、``dlv-29767b5b``）都跑完了实现与验证，才在推送
时撞上 403：token 的 Contents 是 write，但改 ``.github/workflows/`` 需要
Workflows 写权限。诊断落在最贵的一步之后，代价是两次完整的实现预算。

这里把同一组判断提前到开工前，并区分三种结论：

- ``ok``：有证据表明这一步能做；
- ``warn``：不阻断，但这一项**没有证据**（例如没做 workflow 写权限探测）；
- ``blocked``：现在就推不出去，别开始。

自检只读。唯一的写操作是 ``--probe-workflows`` 显式要求的 capability 探测：
建一个不被任何 ref 引用的 tree。GitHub 只在 tree 里出现 ``.github/workflows/``
下的新内容时才检查 Workflows 写权限，所以离线无法判断。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from apps.code_review_pipeline.routing.github_client import GitHubRepo

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_BLOCKED = "blocked"

#: 仓库级权限里能推代码的三种。``permissions.push`` 是同一事实的布尔说法，
#: 两个字段都看：细粒度 token 与经典 token 的响应并不总是都带。
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})

_DETAIL_LIMIT = 300


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:_DETAIL_LIMIT]


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class PreflightReport:
    repo: str
    checks: tuple[PreflightCheck, ...]

    @property
    def blocked(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.status == STATUS_BLOCKED)

    @property
    def ok(self) -> bool:
        return not self.blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
        }


def _split_repo(repo: str) -> GitHubRepo:
    owner, _, name = (repo or "").strip().partition("/")
    if not owner or not name:
        raise ValueError(f"repo 需要 owner/name 形式: {repo!r}")
    return GitHubRepo(owner, name)


async def _check_identity(github: Any, target: GitHubRepo) -> PreflightCheck:
    try:
        login = await github.get_authenticated_login()
    except Exception as exc:  # noqa: BLE001 - 任何失败都意味着现在不能开工
        return PreflightCheck("token", STATUS_BLOCKED, _short(exc))
    if not login:
        return PreflightCheck("token", STATUS_BLOCKED, "GitHub 没有返回登录名")
    return PreflightCheck("token", STATUS_OK, f"身份 {login}")


async def _check_push(github: Any, target: GitHubRepo) -> PreflightCheck:
    try:
        info = await github.get_repo(target)
    except Exception as exc:  # noqa: BLE001
        return PreflightCheck("repo", STATUS_BLOCKED, _short(exc))
    permissions = dict(info.get("permissions") or {})
    permission = str(info.get("permission") or "")
    if permissions.get("push") or permission in _WRITE_PERMISSIONS:
        return PreflightCheck(
            "push",
            STATUS_OK,
            f"仓库写权限 permission={permission or 'push'}",
        )
    return PreflightCheck(
        "push",
        STATUS_BLOCKED,
        f"token 对该仓库没有写权限（permission={permission or 'unknown'}）；"
        "交付会在推送那一步被拒",
    )


async def _check_branch(
    github: Any, target: GitHubRepo, base_branch: str
) -> tuple[PreflightCheck, str]:
    try:
        sha = await github.get_branch_sha(target, base_branch)
    except Exception as exc:  # noqa: BLE001
        return PreflightCheck("branch", STATUS_BLOCKED, _short(exc)), ""
    if not sha:
        return (
            PreflightCheck(
                "branch", STATUS_BLOCKED, f"远端没有 {base_branch} 分支"
            ),
            "",
        )
    return PreflightCheck("branch", STATUS_OK, f"{base_branch}={sha[:12]}"), sha


async def _check_workflows(
    github: Any,
    target: GitHubRepo,
    *,
    base_sha: str,
    probe: bool,
) -> PreflightCheck:
    if not probe:
        return PreflightCheck(
            "workflows",
            STATUS_WARN,
            "未探测：加 --probe-workflows 会建一个不被任何 ref 引用的 tree，"
            "确认 Workflows 写权限",
        )
    if not base_sha:
        return PreflightCheck(
            "workflows", STATUS_WARN, "base 分支未知，跳过 workflow 写权限探测"
        )
    try:
        commit = await github.get_commit(target, base_sha)
        tree_sha = str(dict(commit.get("tree") or {}).get("sha") or "")
        if not tree_sha:
            raise RuntimeError(f"commit {base_sha[:12]} 没有 tree")
        await github.probe_workflow_write(target, base_tree=tree_sha)
    except Exception as exc:  # noqa: BLE001
        return PreflightCheck("workflows", STATUS_BLOCKED, _short(exc))
    return PreflightCheck(
        "workflows",
        STATUS_OK,
        "可以写 .github/workflows/（探测 tree 未被任何 ref 引用）",
    )


#: flow 层调用 preflight 时的哨兵： DeliveryFlow 不持有 MergeStore（它在 adapter
#: 的发布路径上），这项检查归 ``delivery run`` 的 CLI 装配层负责。传这个值表示
#: "调用方无法回答"，如实记 warn，而不是误报 blocked。
MERGE_STORE_UNCHECKED = object()


def _check_merge_store(merge_store: Any) -> PreflightCheck:
    if merge_store is MERGE_STORE_UNCHECKED:
        return PreflightCheck(
            "merge_store",
            STATUS_WARN,
            "flow 层未携带 MergeStore，此检查由 delivery run 的装配层负责",
        )
    if merge_store is None:
        return PreflightCheck(
            "merge_store",
            STATUS_BLOCKED,
            "没有共享 MergeStore；交付在推送后无法登记合并",
        )
    from apps.code_review_pipeline.merge_store import PostgresMergeStore

    if not isinstance(merge_store, PostgresMergeStore):
        return PreflightCheck(
            "merge_store",
            STATUS_BLOCKED,
            "当前是内存 MergeStore；delivery run 要求共享库（VECTOR_DB_DSN）",
        )
    try:
        merge_store.list_open()
    except Exception as exc:  # noqa: BLE001
        return PreflightCheck("merge_store", STATUS_BLOCKED, _short(exc))
    return PreflightCheck("merge_store", STATUS_OK, "合并通道数据库可达")


async def run_preflight(
    repo: str,
    *,
    github: Any,
    base_branch: str = "main",
    merge_store: Any = None,
    probe_workflows: bool = False,
) -> PreflightReport:
    """跑一遍只读自检。身份或仓库不可用时提前返回——后面的检查都会同样失败。"""
    try:
        target = _split_repo(repo)
    except ValueError as exc:
        return PreflightReport(
            repo=repo,
            checks=(PreflightCheck("repo", STATUS_BLOCKED, str(exc)),),
        )

    identity = await _check_identity(github, target)
    if identity.status == STATUS_BLOCKED:
        return PreflightReport(repo=repo, checks=(identity,))

    push = await _check_push(github, target)
    branch, base_sha = await _check_branch(github, target, base_branch)
    workflows = await _check_workflows(
        github, target, base_sha=base_sha, probe=probe_workflows
    )
    merge = await asyncio.to_thread(_check_merge_store, merge_store)
    return PreflightReport(
        repo=repo,
        checks=(identity, push, branch, workflows, merge),
    )
