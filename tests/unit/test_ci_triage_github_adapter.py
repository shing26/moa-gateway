from __future__ import annotations

import httpx
import pytest

from apps.code_review_pipeline.delivery.adapters.github_ci import (
    CiNotFailed,
    GitHubCiEvidenceCollector,
)
from apps.code_review_pipeline.routing.github_client import (
    CI_FAILURE,
    CI_SUCCESS,
    CiCheck,
    CiStatus,
    GitHubRepo,
)


SHA = "a" * 40


class FakeGitHub:
    def __init__(self) -> None:
        self.log_requests: list[int] = []
        self.job_requests: list[int] = []
        self.status = CiStatus(
            state=CI_FAILURE,
            checks=(
                CiCheck(
                    name="test",
                    conclusion="failure",
                    url="https://github.com/owner/name/actions/runs/2",
                ),
            ),
        )
        self.runs: list[dict] = []
        self.jobs: dict[int, list[dict]] = {}
        self.logs: dict[int, str] = {}

    async def get_ci_status(self, repo: GitHubRepo, ref: str) -> CiStatus:
        assert repo == GitHubRepo("owner", "name")
        assert ref == SHA
        return self.status

    async def list_workflow_runs_for_sha(
        self, repo: GitHubRepo, head_sha: str
    ) -> list[dict]:
        assert repo == GitHubRepo("owner", "name")
        assert head_sha == SHA
        return self.runs

    async def list_workflow_jobs(self, repo: GitHubRepo, run_id: int) -> list[dict]:
        assert repo == GitHubRepo("owner", "name")
        self.job_requests.append(run_id)
        return self.jobs[run_id]

    async def get_job_log(
        self, repo: GitHubRepo, job_id: int
    ) -> str:
        assert repo == GitHubRepo("owner", "name")
        self.log_requests.append(job_id)
        return self.logs[job_id]


@pytest.mark.asyncio
async def test_collector_uses_latest_failed_run_and_redacts_logs() -> None:
    github = FakeGitHub()
    github.runs = [
        {
            "id": 1,
            "workflow_id": 10,
            "name": "CI",
            "run_number": 1,
            "run_attempt": 1,
            "conclusion": "failure",
        },
        {
            "id": 2,
            "workflow_id": 10,
            "name": "CI",
            "run_number": 2,
            "run_attempt": 1,
            "conclusion": "failure",
        },
    ]
    github.jobs = {
        1: [{"id": 11, "name": "stale", "conclusion": "failure"}],
        2: [{"id": 22, "name": "pytest", "conclusion": "failure"}],
    }
    github.logs = {
        11: "old failure",
        22: "FAILED tests/test_api.py\nAuthorization: Bearer ghp_secretsecretsecretsecret\n",
    }
    collector = GitHubCiEvidenceCollector(
        github=github,
        max_jobs=5,
        max_log_chars=2_000,
    )

    evidence = await collector.collect(repo="owner/name", head_sha=SHA)

    assert github.job_requests == [2]
    assert github.log_requests == [22]
    assert len(evidence.jobs) == 1
    assert evidence.jobs[0].name == "pytest"
    assert "ghp_" not in evidence.jobs[0].log
    assert "FAILED tests/test_api.py" in evidence.jobs[0].log


@pytest.mark.asyncio
async def test_collector_redacts_before_clipping_job_log_tail() -> None:
    token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    github = FakeGitHub()
    github.runs = [
        {
            "id": 2,
            "workflow_id": 10,
            "name": "CI",
            "run_number": 2,
            "run_attempt": 1,
            "conclusion": "failure",
        },
    ]
    github.jobs = {
        2: [{"id": 22, "name": "pytest", "conclusion": "failure"}],
    }
    github.logs = {
        22: (
            "collected 10 items\n"
            f"Authorization: Bearer {token}\n"
            "FAILED tests/test_api.py - AssertionError\n"
        ),
    }
    collector = GitHubCiEvidenceCollector(
        github=github,
        max_log_chars=100,
    )

    evidence = await collector.collect(repo="owner/name", head_sha=SHA)

    log = evidence.jobs[0].log
    assert len(log) <= 100
    assert token[-12:] not in log
    assert "FAILED tests/test_api.py" in log


@pytest.mark.asyncio
async def test_collector_keeps_checks_when_actions_permission_is_missing() -> None:
    class ForbiddenGitHub(FakeGitHub):
        async def list_workflow_runs_for_sha(
            self, repo: GitHubRepo, head_sha: str
        ) -> list[dict]:
            request = httpx.Request("GET", "https://api.github.com/actions/runs")
            response = httpx.Response(403, request=request, json={"message": "forbidden"})
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    collector = GitHubCiEvidenceCollector(github=ForbiddenGitHub())

    evidence = await collector.collect(repo="owner/name", head_sha=SHA)

    assert evidence.checks[0].name == "test"
    assert evidence.jobs == ()
    assert "Actions: Read" in evidence.actions_unavailable_reason


@pytest.mark.asyncio
async def test_collector_rejects_green_ci() -> None:
    github = FakeGitHub()
    github.status = CiStatus(state=CI_SUCCESS, checks=())
    collector = GitHubCiEvidenceCollector(github=github)

    with pytest.raises(CiNotFailed, match="success"):
        await collector.collect(repo="owner/name", head_sha=SHA)
