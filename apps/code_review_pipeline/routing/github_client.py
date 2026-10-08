from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"


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
    if combined_status:
        state = str(combined_status.get("state") or "").lower()
        if state:
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

    async def get_ci_status(self, repo: GitHubRepo, ref: str) -> CiStatus:
        """读某个 commit 的 CI 结论（两套 API 合并，见 ``aggregate_ci_status``）。

        ``ref`` 用**完整 sha**，不用分支名：分支名会在读取过程中移动，而这里要判断
        的是"这个 PR 的这个版本"能不能合并。

        check-runs 必须翻页。GitHub 默认每页 30 条，而 ``per_page`` 上限 100——
        只取第一页会让"第 101 条红了"被静默漏掉。``list_reviews`` 已经吃过这个亏
        （见其 docstring），同一个坑不再踩第二次。
        """
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
                break
            page += 1

        status_resp = await self._client.get(
            f"/repos/{repo.owner}/{repo.name}/commits/{ref}/status"
        )
        status_resp.raise_for_status()
        return aggregate_ci_status(check_runs, dict(status_resp.json()))

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

    @staticmethod
    def from_env() -> GitHubClient:
        from app.config import settings

        token = settings.github_token
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required for Code Review Pipeline")
        return GitHubClient(token=token)
