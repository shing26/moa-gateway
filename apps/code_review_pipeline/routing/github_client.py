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

    @staticmethod
    def from_env() -> GitHubClient:
        from app.config import settings

        token = settings.github_token
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required for Code Review Pipeline")
        return GitHubClient(token=token)
