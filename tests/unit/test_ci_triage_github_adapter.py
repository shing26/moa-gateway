from __future__ import annotations

import httpx
import pytest

from apps.code_review_pipeline.delivery.adapters.github_ci import (
    CiNotFailed,
    GitHubCiEvidenceCollector,
    _run_ids_from_checks,
)
from apps.code_review_pipeline.delivery.ci_triage import build_ci_triage_report
from apps.code_review_pipeline.merge_state import MergeState
from apps.code_review_pipeline.merge_store import MergeRecord
from apps.code_review_pipeline.notifications.merge_notifier import MergeNotifier
from apps.code_review_pipeline.routing.github_client import (
    CI_FAILURE,
    CI_SUCCESS,
    CiCheck,
    CiStatus,
    GitHubRepo,
    JobLogTooLarge,
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
        self.job_errors: dict[int, Exception] = {}

    async def get_check_run_status(self, repo: GitHubRepo, ref: str) -> CiStatus:
        assert repo == GitHubRepo("owner", "name")
        assert ref == SHA
        return self.status

    async def get_ci_status(self, repo: GitHubRepo, ref: str) -> CiStatus:
        raise AssertionError("CI triage must not read legacy commit statuses")

    async def list_workflow_runs_for_sha(
        self, repo: GitHubRepo, head_sha: str
    ) -> list[dict]:
        assert repo == GitHubRepo("owner", "name")
        assert head_sha == SHA
        return self.runs

    async def list_workflow_jobs(self, repo: GitHubRepo, run_id: int) -> list[dict]:
        assert repo == GitHubRepo("owner", "name")
        self.job_requests.append(run_id)
        if run_id in self.job_errors:
            raise self.job_errors[run_id]
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
async def test_collector_marks_oversized_job_log_unavailable() -> None:
    class OversizedGitHub(FakeGitHub):
        async def get_job_log(self, repo: GitHubRepo, job_id: int) -> str:
            raise JobLogTooLarge("job log exceeds the safe streaming limit")

    github = OversizedGitHub()
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
    collector = GitHubCiEvidenceCollector(github=github)

    evidence = await collector.collect(repo="owner/name", head_sha=SHA)

    assert evidence.jobs[0].log == ""
    assert "safe streaming limit" in evidence.jobs[0].unavailable_reason


@pytest.mark.asyncio
async def test_collector_rejects_green_ci() -> None:
    github = FakeGitHub()
    github.status = CiStatus(state=CI_SUCCESS, checks=())
    collector = GitHubCiEvidenceCollector(github=github)

    with pytest.raises(CiNotFailed, match="success"):
        await collector.collect(repo="owner/name", head_sha=SHA)


@pytest.mark.asyncio
async def test_collector_uses_check_url_run_when_sha_lookup_is_empty() -> None:
    github = FakeGitHub()
    github.status = CiStatus(
        state=CI_FAILURE,
        checks=(
            CiCheck(
                name="test",
                conclusion="failure",
                url="https://github.com/owner/name/actions/runs/123/job/9",
            ),
            CiCheck(
                name="external",
                conclusion="failure",
                url="https://ci.example.com/build/7",
            ),
            CiCheck(name="empty", conclusion="failure", url=""),
        ),
    )
    github.runs = []
    github.jobs = {
        123: [
            {
                "id": 9,
                "name": "pytest",
                "conclusion": "failure",
                "html_url": "https://github.com/owner/name/actions/runs/123/job/9",
            }
        ]
    }
    github.logs = {9: "FAILED tests/test_x.py - AssertionError: 1 == 2"}
    collector = GitHubCiEvidenceCollector(github=github)

    evidence = await collector.collect(repo="owner/name", head_sha=SHA)

    assert github.job_requests == [123]
    assert github.log_requests == [9]
    assert len(evidence.jobs) == 1
    assert evidence.jobs[0].run_id == 123
    assert "FAILED tests/test_x.py" in evidence.jobs[0].log
    assert evidence.actions_unavailable_reason == ""


@pytest.mark.asyncio
async def test_collector_accepts_running_run_with_failed_job() -> None:
    github = FakeGitHub()
    github.status = CiStatus(
        state=CI_FAILURE,
        checks=(
            CiCheck(
                name="test",
                conclusion="failure",
                url="https://github.com/owner/name/actions/runs/123",
            ),
        ),
    )
    github.runs = [
        {
            "id": 123,
            "workflow_id": 10,
            "name": "CI",
            "run_number": 1,
            "run_attempt": 1,
            "conclusion": None,
            "status": "in_progress",
        },
    ]
    github.jobs = {
        123: [{"id": 9, "name": "pytest", "conclusion": "failure"}],
    }
    github.logs = {9: "FAILED tests/test_x.py - AssertionError: 1 == 2"}
    collector = GitHubCiEvidenceCollector(github=github)

    evidence = await collector.collect(repo="owner/name", head_sha=SHA)

    assert len(evidence.jobs) == 1
    assert evidence.jobs[0].name == "pytest"
    assert evidence.actions_unavailable_reason == ""


@pytest.mark.asyncio
async def test_collector_retries_empty_jobs_with_injected_wait() -> None:
    github = FakeGitHub()
    github.status = CiStatus(
        state=CI_FAILURE,
        checks=(
            CiCheck(
                name="test",
                conclusion="failure",
                url="https://github.com/owner/name/actions/runs/123",
            ),
        ),
    )
    github.runs = []
    github.jobs = {123: []}
    waits: list[float] = []

    async def fake_wait(delay: float) -> None:
        waits.append(delay)

    collector = GitHubCiEvidenceCollector(github=github, wait=fake_wait)

    evidence = await collector.collect(repo="owner/name", head_sha=SHA)

    assert github.job_requests == [123, 123, 123]
    assert waits == [10, 10]
    assert evidence.jobs == ()
    assert "no failed job details" in evidence.actions_unavailable_reason


def test_d2_a9_run_ids_require_github_host_and_repo() -> None:
    checks = (
        CiCheck(
            name="external-actions-shape",
            conclusion="failure",
            url="https://ci.example.com/actions/runs/789/job/9",
        ),
        CiCheck(
            name="other-repo",
            conclusion="failure",
            url="https://github.com/other/repo/actions/runs/789/job/9",
        ),
        CiCheck(
            name="target",
            conclusion="failure",
            url="https://github.com/owner/name/actions/runs/123/job/9",
        ),
        CiCheck(
            name="not-https",
            conclusion="failure",
            url="http://github.com/owner/name/actions/runs/456/job/9",
        ),
        CiCheck(name="empty", conclusion="failure", url=""),
    )

    assert _run_ids_from_checks(checks, GitHubRepo("owner", "name")) == [123]


@pytest.mark.asyncio
async def test_d2_a10_partial_run_failure_keeps_collected_jobs() -> None:
    github = FakeGitHub()
    github.status = CiStatus(
        state=CI_FAILURE,
        checks=(
            CiCheck(
                name="pytest",
                conclusion="failure",
                url="https://github.com/owner/name/actions/runs/123/job/9",
            ),
            CiCheck(
                name="external",
                conclusion="failure",
                url="https://ci.example.com/actions/runs/789/job/9",
            ),
        ),
    )
    github.runs = [
        {
            "id": 789,
            "workflow_id": 11,
            "name": "external",
            "run_number": 1,
            "run_attempt": 1,
            "conclusion": None,
            "status": "in_progress",
        },
        {
            "id": 123,
            "workflow_id": 10,
            "name": "CI",
            "run_number": 1,
            "run_attempt": 1,
            "conclusion": None,
            "status": "in_progress",
        },
    ]
    github.jobs = {
        123: [
            {
                "id": 9,
                "name": "pytest",
                "conclusion": "failure",
                "html_url": "https://github.com/owner/name/actions/runs/123/job/9",
            }
        ],
    }
    github.logs = {9: "FAILED tests/test_x.py - AssertionError: 1 == 2"}
    request = httpx.Request("GET", "https://api.github.com/actions/runs/789/jobs")
    response = httpx.Response(404, request=request, json={"message": "not found"})
    github.job_errors = {
        789: httpx.HTTPStatusError(
            "not found", request=request, response=response
        )
    }

    evidence = await GitHubCiEvidenceCollector(github=github).collect(
        repo="owner/name", head_sha=SHA
    )

    assert github.job_requests == [789, 123]
    assert {job.run_id for job in evidence.jobs} == {123}
    assert evidence.actions_unavailable_reason == ""

    report = build_ci_triage_report(
        evidence,
        base_branch="main",
        branch="fix/ci",
        trace_id="trace-1",
    )
    record = MergeRecord(
        repo="owner/name",
        pr_number=42,
        head_sha=SHA,
        state=MergeState.CI_FAILED.value,
        ci_state="failure",
        ci_failing=(
            {
                "name": "pytest",
                "url": "https://github.com/owner/name/actions/runs/123/job/9",
            },
        ),
    )
    card = MergeNotifier(default_target="chat_123")._build_ci_failure_card(
        record, report
    )
    text = "\n".join(
        element.get("content", "")
        for element in card.to_card_payload()["elements"]
        if element.get("tag") == "markdown"
    )

    assert "日志不可用" not in text
    assert "FAILED tests/test_x.py" in text


def test_run_ids_from_checks_skips_non_actions_and_empty_urls() -> None:
    checks = (
        CiCheck(
            name="a",
            conclusion="failure",
            url="https://github.com/owner/name/actions/runs/123/job/9",
        ),
        CiCheck(
            name="b",
            conclusion="failure",
            url="https://github.com/owner/name/actions/runs/123",
        ),
        CiCheck(
            name="c",
            conclusion="failure",
            url="https://ci.example.com/build/7",
        ),
        CiCheck(name="d", conclusion="failure", url=""),
    )

    assert _run_ids_from_checks(checks, GitHubRepo("owner", "name")) == [123]
