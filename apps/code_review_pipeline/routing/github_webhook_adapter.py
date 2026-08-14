from __future__ import annotations

import dataclasses
import logging
from typing import Any

from app.models.events import PlatformEvent
from apps.code_review_pipeline.routing.github_client import GitHubClient, GitHubRepo
from apps.code_review_pipeline.schemas.pr_context import PRContext, PRFile
from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding, PipelineResult

logger = logging.getLogger("moa.code_review.webhook")


class UnsupportedGitHubEvent(Exception):
    pass


class PRFetchError(Exception):
    pass


async def build_pr_context_from_github(client: GitHubClient, body: dict[str, Any]) -> PRContext:
    action = str(body.get("action", "")).lower()
    if action not in {"opened", "synchronize", "reopened"}:
        raise UnsupportedGitHubEvent(f"unsupported action={action}")

    pr_payload = dict(body.get("pull_request", {}))
    repo_payload = dict(body.get("repository", {}))
    full_name = str(repo_payload.get("full_name", "")).strip()
    if full_name and "/" in full_name:
        owner, name = full_name.split("/", 1)
    else:
        owner = str(repo_payload.get("owner", {}).get("login", ""))
        name = str(repo_payload.get("name", ""))
    repo = GitHubRepo(owner=owner, name=name)
    pr_number = int(pr_payload.get("number", 0))
    files = await client.get_pr_files(repo, pr_number)

    changed_files = [
        PRFile(
            filename=str(f.get("filename", "")),
            status=str(f.get("status", "")),
            additions=int(f.get("additions", 0)),
            deletions=int(f.get("deletions", 0)),
            changes=int(f.get("changes", 0)),
            patch=str(f.get("patch")) if f.get("patch") is not None else None,
            sha=str(f.get("sha", "")),
            content=str(f.get("patch")) if str(f.get("filename", "")).endswith(".py") and f.get("patch") is not None else None,
        )
        for f in files
    ]

    return PRContext(
        repo=f"{repo.owner}/{repo.name}",
        pr_number=pr_number,
        head_sha=str(pr_payload.get("head", {}).get("sha", "")),
        base_sha=str(pr_payload.get("base", {}).get("sha", "")),
        title=str(pr_payload.get("title", "")),
        author=str(pr_payload.get("user", {}).get("login", "")),
        html_url=str(pr_payload.get("html_url", "")),
        diff_url=str(pr_payload.get("diff_url", "")),
        changed_files=changed_files,
        labels=tuple(str(label.get("name", "")) for label in pr_payload.get("labels", [])),
        reviewers=tuple(str(r.get("login", "")) for r in pr_payload.get("requested_reviewers", [])),
    )


def dummy_pipeline_result(*, trace_id: str, pr: PRContext, reason: str) -> PipelineResult:
    empty = AgentFindingResult(agent="empty", trace_id=trace_id, findings=(), summary=reason, recommendation="none")
    return PipelineResult(
        trace_id=trace_id,
        pr=pr,
        triage=empty,
        static_analysis=empty,
        semantic_review=empty,
        test_coverage=empty,
        report=empty,
    )
