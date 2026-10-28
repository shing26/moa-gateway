from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from apps.code_review_pipeline.archive import extract_repo_tarball
from apps.code_review_pipeline.change.refs import PullRequestRef

GITHUB_API = "https://api.github.com"


class JobLogTooLarge(RuntimeError):
    """A job log exceeded the bounded streaming collection limit."""


def _trust_env_default() -> bool:
    # 默认**不走**系统代理。实测（2026-10-03）：本机代理在 127.0.0.1:31181 上做
    # TLS 拦截，而它的根证书不在信任链里（SSL_CERT_FILE 指向 anaconda 的
    # cacert.pem），于是 httpx 默认的 trust_env=True 会让每一次 GitHub 调用死在
    # CERTIFICATE_VERIFY_FAILED；trust_env=False 直连是通的（HTTP 200）。
    # 需要代理的环境用 CODE_REVIEW_GITHUB_TRUST_ENV=1 显式打开。
    raw = os.getenv("CODE_REVIEW_GITHUB_TRUST_ENV", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GitHubRepo:
    owner: str
    name: str


# CI 结论只有这四种。分散成字符串常量而不是 Enum：它们要直接进日志与飞书卡片，
# 字符串在两边都不需要转换。
CI_SUCCESS = "success"
CI_FAILURE = "failure"
CI_PENDING = "pending"
# 一条 check 都没有。**不并入 success**：仓库没配 CI 时"全绿"是空洞的真，
# 把它当成绿会让网关在没有测试的仓库上照样弹合并卡片。
CI_NONE = "none"

# 这些 conclusion 表示"这条没通过"。注意 `neutral` / `skipped` **不在此列**：
# 前者是"看了但没意见"，后者是"这条不适用"，把它们算失败会让正常仓库永远合不了。
_FAILING_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "stale",
        "startup_failure",
        # Commit Status API 的失败取值之一（另一套 API 用 `failure`）。
        "error",
    }
)
_PENDING_CONCLUSIONS = frozenset(
    {"queued", "in_progress", "waiting", "requested", "pending"}
)


def is_failing_ci_conclusion(conclusion: str) -> bool:
    """Whether a GitHub check/job conclusion represents a failed result."""
    return str(conclusion or "").strip().lower() in _FAILING_CONCLUSIONS


@dataclass(frozen=True)
class CiCheck:
    """一条 CI 结论。`name` 是给飞书卡片用的：红的时候要能说出"哪一条红了"。"""

    name: str
    conclusion: str
    url: str = ""


@dataclass(frozen=True)
class CiStatus:
    state: str
    checks: tuple[CiCheck, ...] = ()

    @property
    def is_green(self) -> bool:
        return self.state == CI_SUCCESS

    @property
    def failing(self) -> tuple[CiCheck, ...]:
        return tuple(c for c in self.checks if c.conclusion in _FAILING_CONCLUSIONS)

    @property
    def waiting(self) -> tuple[CiCheck, ...]:
        return tuple(c for c in self.checks if c.conclusion in _PENDING_CONCLUSIONS)


def aggregate_ci_status(
    check_runs: list[dict[str, Any]] | None = None,
    combined_status: dict[str, Any] | None = None,
) -> CiStatus:
    """把两套 API 的原始响应聚合成一个结论。**纯函数**，可以直接单测。

    为什么要看两套：GitHub 的 CI 状态分散在

    * **Check Runs**（Actions 时代）：每条有 ``status`` + ``conclusion``；
    * **Commit Status**（更早的 ``statuses``）：整体一个 ``state``。

    一个仓库可能同时用 Actions 和第三方 status（覆盖率、安全扫描）。只看一边，
    "另一边红了"就会被漏掉——那正是"CI 明明是红的却弹了合并卡片"的机制。

    优先级是 **失败 > 进行中 > 成功 > 无**。失败优先是刻意的：3 条绿 + 1 条红必须
    判红。反过来先看"有没有进行中"的话，一条已经失败的 check 会被一条还在跑的
    check 掩盖，结论要么晚弹、要么在失败的情况下弹出合并卡片。
    """
    checks: list[CiCheck] = []

    for run in check_runs or ():
        name = str(run.get("name") or run.get("id") or "check")
        raw_status = str(run.get("status") or "").lower()
        raw_conclusion = str(run.get("conclusion") or "").lower()
        # 未完成时 GitHub 的 `conclusion` 是 null，此时 `status` 才是结论。
        conclusion = raw_conclusion or raw_status
        url = str(run.get("details_url") or run.get("html_url") or "")
        checks.append(CiCheck(name=name, conclusion=conclusion, url=url))

    # Commit Status 的整体 state 已被 GitHub 聚合过，直接当成一条 check 参与判定。
    #
    # 但**空 status 不是"进行中"**：对一个完全没有 status 的 commit，GitHub 返回
    # ``{"state": "pending", "total_count": 0, "statuses": []}``——这个 pending 是
    # 默认值，不代表任何真实工作。把它算成一条 pending check，会让"check-runs 全绿"
    # 的 commit 永远判不成绿。实测（2026-10-09）真实 PR #1 卡在 ``ci_failed`` 正是
    # 这个原因：``test=success``、``docker=skipped``，却因为这条假 pending 被判红。
    #
    # 只在 ``total_count`` **显式**为 0 时跳过：调用方没给这个字段时维持原语义
    # （见 ``test_commit_status_pending``——那条用例传的是 ``{"state": "pending"}``）。
    if combined_status:
        state = str(combined_status.get("state") or "").lower()
        if state and combined_status.get("total_count") != 0:
            checks.append(
                CiCheck(
                    name="commit-status",
                    conclusion=state,
                    url=str(combined_status.get("target_url") or ""),
                )
            )

    if not checks:
        return CiStatus(state=CI_NONE)
    if any(c.conclusion in _FAILING_CONCLUSIONS for c in checks):
        return CiStatus(state=CI_FAILURE, checks=tuple(checks))
    if any(c.conclusion in _PENDING_CONCLUSIONS for c in checks):
        return CiStatus(state=CI_PENDING, checks=tuple(checks))
    return CiStatus(state=CI_SUCCESS, checks=tuple(checks))


@dataclass(frozen=True)
class MergeOutcome:
    """合并结果。

    ``status`` 的取值都是**可预期**的结果，不是异常：冲突、sha 变了、token 没权限
    都会如实告诉人，而不是变成 500。``merged`` 只表示"GitHub 说合了"。
    """

    merged: bool
    status: str
    message: str = ""
    sha: str = ""


def _gh_message(resp: httpx.Response) -> str:
    """从错误响应里取 GitHub 的原话（截断），用于如实转告用户。"""
    try:
        body = resp.json()
        text = str(body.get("message") or "")
    except Exception:
        text = ""
    return (text or resp.text or "").strip()[:300]


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: float = 15.0,
        trust_env: bool | None = None,
    ) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "moa-code-review-pipeline",
            },
            timeout=httpx.Timeout(timeout),
            trust_env=_trust_env_default() if trust_env is None else trust_env,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_pr_files(self, repo: GitHubRepo, pr_number: int) -> list[dict[str, Any]]:
        resp = await self._client.get(
            f"/repos/{repo.owner}/{repo.name}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )
        resp.raise_for_status()
        return list(resp.json())

    async def get_pr(self, repo: GitHubRepo, pr_number: int) -> dict[str, Any]:
        resp = await self._client.get(f"/repos/{repo.owner}/{repo.name}/pulls/{pr_number}")
        resp.raise_for_status()
        return dict(resp.json())

    async def download_archive(self, repo: GitHubRepo, ref: str, dest: Path) -> None:
        """下载某个 commit 的 tarball 并解压到 ``dest``。

        用归档 API 而不是 ``git clone``：token 不会出现在命令行里，工作区也不需要
        依赖宿主机的 git 配置。GitHub 会在 302 后给出实际归档地址，因此必须跟随
        重定向。
        """
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix="moa-gh-archive-", suffix=".tar.gz")
        os.close(fd)
        archive = Path(raw_path)
        try:
            async with self._client.stream(
                "GET",
                f"/repos/{repo.owner}/{repo.name}/tarball/{ref}",
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                handle = await asyncio.to_thread(archive.open, "wb")
                try:
                    async for chunk in response.aiter_bytes():
                        await asyncio.to_thread(handle.write, chunk)
                finally:
                    await asyncio.to_thread(handle.close)
            await asyncio.to_thread(extract_repo_tarball, archive, dest)
        finally:
            archive.unlink(missing_ok=True)

    async def list_reviews(self, repo: GitHubRepo, pr_number: int) -> list[dict[str, Any]]:
        """列出 PR 上的 review（D4 幂等兜底要用）。

        **必须翻页**：GitHub 默认每页 30 条，而 ``find_marked_review`` 只在前一页里
        找 marker 的话，一个审过 40 个 PR 的仓库上，兜底会在第 31 条之后失效——
        于是那个窗口重新变成"会重复评论"，而且只在老仓库上出现，极难复现。
        """
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._client.get(
                f"/repos/{repo.owner}/{repo.name}/pulls/{pr_number}/reviews",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = list(resp.json())
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    async def create_review(
        self,
        repo: GitHubRepo,
        pr_number: int,
        body: str,
        *,
        event: str = "COMMENT",
    ) -> dict[str, Any]:
        """在 PR 上发一条 review（D4 写回）。

        ``event=COMMENT``：只留评论、不 REQUEST_CHANGES / APPROVE。
        刻意不用 REQUEST_CHANGES——那是**替人做合并决策**，而这套系统的立场是
        给出可核查的发现、由人决定要不要挡。越权动作不该由自动流程代劳。
        """
        resp = await self._client.post(
            f"/repos/{repo.owner}/{repo.name}/pulls/{pr_number}/reviews",
            json={"body": body, "event": event},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def _list_check_runs_for_sha(
        self, repo: GitHubRepo, ref: str
    ) -> list[dict[str, Any]]:
        check_runs: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._client.get(
                f"/repos/{repo.owner}/{repo.name}/commits/{ref}/check-runs",
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            batch = list(resp.json().get("check_runs") or ())
            check_runs.extend(batch)
            if len(batch) < 100:
                return check_runs
            page += 1

    async def get_check_run_status(self, repo: GitHubRepo, ref: str) -> CiStatus:
        """Read check-runs only, without the legacy Commit Statuses API."""
        check_runs = await self._list_check_runs_for_sha(repo, ref)
        return aggregate_ci_status(check_runs)

    async def get_ci_status(self, repo: GitHubRepo, ref: str) -> CiStatus:
        """读某个 commit 的 CI 结论（两套 API 合并，见 ``aggregate_ci_status``）。

        ``ref`` 用**完整 sha**，不用分支名：分支名会在读取过程中移动，而这里要判断
        的是"这个 PR 的这个版本"能不能合并。

        check-runs 必须翻页。GitHub 默认每页 30 条，而 ``per_page`` 上限 100——
        只取第一页会让"第 101 条红了"被静默漏掉。``list_reviews`` 已经吃过这个亏
        （见其 docstring），同一个坑不再踩第二次。
        """
        check_runs = await self._list_check_runs_for_sha(repo, ref)
        status_resp = await self._client.get(
            f"/repos/{repo.owner}/{repo.name}/commits/{ref}/status"
        )
        status_resp.raise_for_status()
        return aggregate_ci_status(check_runs, dict(status_resp.json()))

    async def list_workflow_runs_for_sha(
        self, repo: GitHubRepo, head_sha: str
    ) -> list[dict[str, Any]]:
        """List Actions workflow runs for one full commit SHA.

        This is read-only evidence collection. Callers must not infer a failing
        job from a check-run id: only Actions run/job ids can address job logs.
        """
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._client.get(
                f"/repos/{repo.owner}/{repo.name}/actions/runs",
                params={
                    "head_sha": head_sha,
                    "per_page": 100,
                    "page": page,
                },
            )
            resp.raise_for_status()
            batch = list(resp.json().get("workflow_runs") or ())
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    async def list_workflow_jobs(
        self, repo: GitHubRepo, run_id: int
    ) -> list[dict[str, Any]]:
        """List the latest attempt's jobs for one workflow run."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._client.get(
                f"/repos/{repo.owner}/{repo.name}/actions/runs/{run_id}/jobs",
                params={
                    "filter": "latest",
                    "per_page": 100,
                    "page": page,
                },
            )
            resp.raise_for_status()
            batch = list(resp.json().get("jobs") or ())
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    async def get_job_log(
        self,
        repo: GitHubRepo,
        job_id: int,
        *,
        max_bytes: int = 2_000_000,
    ) -> str:
        """Download a job log, following GitHub's redirect to blob storage."""
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        async with self._client.stream(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/actions/jobs/{job_id}/logs",
            follow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            content = bytearray()
            async for chunk in resp.aiter_bytes():
                if len(content) + len(chunk) > max_bytes:
                    raise JobLogTooLarge(
                        f"job log exceeds the {max_bytes}-byte streaming limit"
                    )
                content.extend(chunk)
            return bytes(content).decode(
                resp.encoding or "utf-8",
                errors="replace",
            )

    async def merge_pr(
        self,
        repo: GitHubRepo,
        pr_number: int,
        *,
        sha: str = "",
        commit_title: str = "",
    ) -> MergeOutcome:
        """合并 PR。

        **不传 `merge_method`**：用仓库自己配置的默认方式（merge / squash /
        rebase）。网关不该替用户决定历史长什么样。

        ``sha`` 传了就让 GitHub 做乐观锁：HEAD 变了则拒绝（409）。这是本方法最要紧
        的一条——审批是在**某个 sha** 上做出的，如果审批期间又推了新 commit，那"人
        批准的那个版本"已经不存在了。传 sha 保证绝不会悄悄合掉一个没人审过的版本。
        """
        payload: dict[str, Any] = {}
        if sha:
            payload["sha"] = sha
        if commit_title:
            payload["commit_title"] = commit_title

        resp = await self._client.put(
            f"/repos/{repo.owner}/{repo.name}/pulls/{pr_number}/merge",
            json=payload,
        )
        # 405 / 409 / 403 都是**可预期**的结果，不是异常：冲突或被保护分支拒绝、
        # sha 已经变了、token 没有合并权限。上层要把原因如实转告人，而不是抛成 500
        # 让人在日志里找。
        if resp.status_code == 405:
            return MergeOutcome(merged=False, status="not_mergeable", message=_gh_message(resp))
        if resp.status_code == 409:
            return MergeOutcome(merged=False, status="sha_mismatch", message=_gh_message(resp))
        if resp.status_code == 403:
            return MergeOutcome(merged=False, status="forbidden", message=_gh_message(resp))
        resp.raise_for_status()
        body = dict(resp.json())
        merged = bool(body.get("merged"))
        return MergeOutcome(
            merged=merged,
            status="merged" if merged else "refused",
            message=str(body.get("message") or ""),
            sha=str(body.get("sha") or ""),
        )

    # ── 写侧（ADR-030）：只做建分支 / 建 commit / 开 PR ───────────────────

    async def commit_exists(self, repo: GitHubRepo, sha: str) -> bool:
        """远端是否存在该 commit。404 = 不存在；其它错误照常抛出。"""
        resp = await self._client.get(
            f"/repos/{repo.owner}/{repo.name}/git/commits/{sha}"
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    async def create_ref(
        self,
        repo: GitHubRepo,
        branch: str,
        base_sha: str,
    ) -> dict[str, Any]:
        """从 ``base_sha`` 建一个分支。已存在时 GitHub 返回 422，如实抛出。"""
        resp = await self._client.post(
            f"/repos/{repo.owner}/{repo.name}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        resp.raise_for_status()
        return dict(resp.json())

    async def create_commit(
        self,
        repo: GitHubRepo,
        *,
        base_sha: str,
        branch: str,
        message: str,
        files: list[dict[str, str]],
    ) -> str:
        """用 Git Data API 建一个 commit，返回新 commit sha。

        ``files`` 每项含 ``path`` / ``content_b64``；删除的文件用 ``sha=None``
        表达。这里**不**走 Contents API：它一次只能写一个文件，多文件改动会变成
        多个 commit，而"一次变更一个 commit"是变更包可复核的前提。
        """
        base_commit = await self._client.get(
            f"/repos/{repo.owner}/{repo.name}/git/commits/{base_sha}"
        )
        base_commit.raise_for_status()
        base_tree = str(dict(base_commit.json()).get("tree", {}).get("sha") or "")
        if not base_tree:
            raise RuntimeError(f"无法读取 base commit 的 tree: {base_sha}")

        entries: list[dict[str, Any]] = []
        for item in files:
            path = str(item.get("path") or "")
            content = item.get("content_b64")
            if content is None:
                # 删除：tree 项不带 sha 即表示该路径不存在
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            blob = await self._client.post(
                f"/repos/{repo.owner}/{repo.name}/git/blobs",
                json={"content": content, "encoding": "base64"},
            )
            blob.raise_for_status()
            entries.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": str(dict(blob.json()).get("sha") or ""),
                }
            )

        tree_resp = await self._client.post(
            f"/repos/{repo.owner}/{repo.name}/git/trees",
            json={"base_tree": base_tree, "tree": entries},
        )
        tree_resp.raise_for_status()
        tree_sha = str(dict(tree_resp.json()).get("sha") or "")

        commit_resp = await self._client.post(
            f"/repos/{repo.owner}/{repo.name}/git/commits",
            json={"message": message, "tree": tree_sha, "parents": [base_sha]},
        )
        commit_resp.raise_for_status()
        commit_sha = str(dict(commit_resp.json()).get("sha") or "")

        # 分支 ref 推进到新 commit（非 force：GitHub 要求 fast-forward）
        ref_resp = await self._client.patch(
            f"/repos/{repo.owner}/{repo.name}/git/refs/heads/{branch}",
            json={"sha": commit_sha, "force": False},
        )
        ref_resp.raise_for_status()
        return commit_sha

    async def create_pull_request(
        self,
        repo: GitHubRepo,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequestRef:
        """Open a PR and return the identity required by the merge channel."""
        resp = await self._client.post(
            f"/repos/{repo.owner}/{repo.name}/pulls",
            json={
                "head": branch,
                "base": base_branch,
                "title": title,
                "body": body,
            },
        )
        resp.raise_for_status()
        payload = dict(resp.json())
        number = int(payload.get("number") or 0)
        url = str(payload.get("html_url") or "")
        head_sha = str(dict(payload.get("head") or {}).get("sha") or "")
        if number <= 0 or not url or not head_sha:
            raise RuntimeError(
                "GitHub create_pull_request response lacks number/url/head.sha"
            )
        return PullRequestRef(number=number, url=url, head_sha=head_sha)

    @staticmethod
    def from_env() -> GitHubClient:
        from app.config import settings

        token = settings.github_token
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required for Code Review Pipeline")
        return GitHubClient(token=token)
