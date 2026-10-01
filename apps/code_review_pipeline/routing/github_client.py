from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class GitHubRepo:
    owner: str
    name: str


class GitHubClient:
    def __init__(self, token: str, *, timeout: float = 15.0) -> None:
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

    @staticmethod
    def from_env() -> GitHubClient:
        from app.config import settings

        token = settings.github_token
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required for Code Review Pipeline")
        return GitHubClient(token=token)
