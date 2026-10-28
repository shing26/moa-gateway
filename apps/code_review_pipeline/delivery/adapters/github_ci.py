"""GitHub Actions evidence collector for read-only CI triage."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from apps.code_review_pipeline.delivery.ci_triage import (
    CiFailureCheck,
    CiFailureEvidence,
    CiJobLog,
    redact_ci_log,
)
from apps.code_review_pipeline.routing.github_client import (
    CI_FAILURE,
    CiStatus,
    GitHubRepo,
    JobLogTooLarge,
    is_failing_ci_conclusion,
)


class CiNotFailed(RuntimeError):
    """The requested commit is not currently in a failing CI state."""


class CiGitHubPort(Protocol):
    async def get_check_run_status(
        self, repo: GitHubRepo, ref: str
    ) -> CiStatus: ...

    async def list_workflow_runs_for_sha(
        self, repo: GitHubRepo, head_sha: str
    ) -> list[dict[str, Any]]: ...

    async def list_workflow_jobs(
        self, repo: GitHubRepo, run_id: int
    ) -> list[dict[str, Any]]: ...

    async def get_job_log(self, repo: GitHubRepo, job_id: int) -> str: ...


def _repo(value: str) -> GitHubRepo:
    owner, separator, name = value.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError(f"repo must be owner/name: {value!r}")
    return GitHubRepo(owner=owner, name=name)


def _run_order(run: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(run.get("run_number") or 0),
        int(run.get("run_attempt") or 0),
        int(run.get("id") or 0),
    )


def _latest_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[object, dict[str, Any]] = {}
    for run in runs:
        key = (
            run.get("workflow_id")
            or run.get("path")
            or run.get("name")
            or run.get("id")
        )
        current = latest.get(key)
        if current is None or _run_order(run) > _run_order(current):
            latest[key] = run
    return list(latest.values())


def _http_reason(exc: httpx.HTTPStatusError, *, what: str) -> str:
    status = exc.response.status_code
    if status in {401, 403}:
        return (
            f"{what} unavailable: HTTP {status}. "
            "Grant fine-grained GitHub 'Actions: Read' and 'Checks: Read' "
            "permissions for this repository."
        )
    if status == 404:
        return f"{what} unavailable: HTTP 404 (not found or permission denied)."
    return f"{what} unavailable: HTTP {status}."


class GitHubCiEvidenceCollector:
    def __init__(
        self,
        *,
        github: CiGitHubPort,
        max_jobs: int = 10,
        max_log_chars: int = 50_000,
    ) -> None:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        if max_log_chars <= 0:
            raise ValueError("max_log_chars must be positive")
        self._github = github
        self._max_jobs = max_jobs
        self._max_log_chars = max_log_chars

    async def collect(self, *, repo: str, head_sha: str) -> CiFailureEvidence:
        target = _repo(repo)
        status = await self._github.get_check_run_status(target, head_sha)
        if status.state != CI_FAILURE:
            raise CiNotFailed(
                f"CI state for {repo}@{head_sha} is {status.state!r}, not failure"
            )
        checks = tuple(
            CiFailureCheck(
                name=check.name,
                conclusion=check.conclusion,
                url=check.url,
            )
            for check in status.failing
        )
        try:
            runs = await self._github.list_workflow_runs_for_sha(target, head_sha)
        except httpx.HTTPStatusError as exc:
            return CiFailureEvidence(
                repo=repo,
                head_sha=head_sha,
                checks=checks,
                actions_unavailable_reason=_http_reason(
                    exc, what="GitHub Actions workflow runs"
                ),
            )

        failed_runs = [
            run
            for run in _latest_runs(runs)
            if is_failing_ci_conclusion(str(run.get("conclusion") or ""))
        ]
        jobs: list[CiJobLog] = []
        limitation = ""
        for run in failed_runs:
            run_id = int(run.get("id") or 0)
            workflow_name = str(
                run.get("name") or run.get("display_title") or run.get("path") or "workflow"
            )
            try:
                raw_jobs = await self._github.list_workflow_jobs(target, run_id)
            except httpx.HTTPStatusError as exc:
                limitation = _http_reason(
                    exc, what=f"GitHub Actions jobs for run {run_id}"
                )
                continue
            for raw in raw_jobs:
                conclusion = str(raw.get("conclusion") or "").lower()
                if not is_failing_ci_conclusion(conclusion):
                    continue
                job_id = int(raw.get("id") or 0)
                try:
                    log = await self._github.get_job_log(target, job_id)
                    unavailable = ""
                except httpx.HTTPStatusError as exc:
                    log = ""
                    unavailable = _http_reason(exc, what=f"job {job_id} log")
                except JobLogTooLarge as exc:
                    log = ""
                    unavailable = f"job {job_id} log unavailable: {exc}"
                jobs.append(
                    CiJobLog(
                        run_id=run_id,
                        job_id=job_id,
                        workflow_name=workflow_name,
                        name=str(raw.get("name") or f"job-{job_id}"),
                        conclusion=conclusion,
                        url=str(raw.get("html_url") or ""),
                        log=redact_ci_log(log)[-self._max_log_chars :],
                        unavailable_reason=unavailable,
                    )
                )
                if len(jobs) >= self._max_jobs:
                    break
            if len(jobs) >= self._max_jobs:
                break

        if failed_runs and not jobs and not limitation:
            limitation = (
                "GitHub reported failed workflow runs, but no failed job details "
                "were available to this token."
            )
        return CiFailureEvidence(
            repo=repo,
            head_sha=head_sha,
            checks=checks,
            jobs=tuple(jobs),
            actions_unavailable_reason=limitation,
            jobs_truncated=len(jobs) >= self._max_jobs,
        )


__all__ = ["CiGitHubPort", "CiNotFailed", "GitHubCiEvidenceCollector"]
