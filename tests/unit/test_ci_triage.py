from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.code_review_pipeline.delivery.ci_triage import (
    CiFailureCheck,
    CiFailureEvidence,
    CiJobLog,
    build_ci_triage_report,
    classify_ci_failure,
    redact_ci_log,
    write_ci_triage_artifacts,
)


SHA = "a" * 40


def _evidence(*jobs: CiJobLog) -> CiFailureEvidence:
    return CiFailureEvidence(
        repo="owner/name",
        head_sha=SHA,
        checks=(
            CiFailureCheck(
                name="test",
                conclusion="failure",
                url="https://github.com/owner/name/actions/runs/1",
            ),
        ),
        jobs=jobs,
    )


def _job(log: str, *, conclusion: str = "failure") -> CiJobLog:
    return CiJobLog(
        run_id=1,
        job_id=2,
        workflow_name="CI",
        name="pytest",
        conclusion=conclusion,
        url="https://github.com/owner/name/actions/runs/1/job/2",
        log=log,
    )


def test_redact_ci_log_removes_tokens_and_authorization_values() -> None:
    raw = (
        "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"
        "token=github_pat_abcdefghijklmnopqrstuvwxyz0123456789\n"
        "normal failure output\n"
    )

    redacted = redact_ci_log(raw)

    assert "ghp_" not in redacted
    assert "github_pat_" not in redacted
    assert "normal failure output" in redacted
    assert redacted.count("[REDACTED]") == 2


def test_redact_ci_log_removes_common_credential_encodings() -> None:
    raw = (
        '{"password":"json-secret-123"}\n'
        "Authorization: Basic dXNlcjpwYXNzd29yZA==\n"
        '{"Authorization":"Bearer serialized-bearer-secret"}\n'
        "Headers({'authorization': 'Bearer mapping-bearer-secret'})\n"
        "Bearer bare-bearer-secret\n"
        'Bearer "quoted-bearer-secret"\n'
        "AWS_SECRET_ACCESS_KEY=aws-secret-456\n"
        "https://alice:url-secret-789@example.com/path\n"
        "postgresql://dbuser:db-secret-321@db.example/app\n"
        "redis://:redis-secret-654@cache.example:6380/2\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "pem-secret-000\n"
        "-----END PRIVATE KEY-----\n"
    )

    redacted = redact_ci_log(raw)

    for secret in (
        "json-secret-123",
        "dXNlcjpwYXNzd29yZA==",
        "serialized-bearer-secret",
        "mapping-bearer-secret",
        "bare-bearer-secret",
        "quoted-bearer-secret",
        "aws-secret-456",
        "url-secret-789",
        "db-secret-321",
        "redis-secret-654",
        "pem-secret-000",
    ):
        assert secret not in redacted


def test_artifacts_do_not_contain_common_credentials(tmp_path: Path) -> None:
    secrets = (
        "json-secret-123",
        "dXNlcjpwYXNzd29yZA==",
        "serialized-bearer-secret",
        "mapping-bearer-secret",
        "bare-bearer-secret",
        "quoted-bearer-secret",
        "aws-secret-456",
        "url-secret-789",
        "db-secret-321",
        "redis-secret-654",
        "pem-secret-000",
    )
    report = build_ci_triage_report(
        _evidence(
            _job(
                "FAILED tests/test_auth.py "
                '{"password":"json-secret-123"} '
                "Authorization: Basic dXNlcjpwYXNzd29yZA== "
                '{"Authorization":"Bearer serialized-bearer-secret"} '
                "Headers({'authorization': 'Bearer mapping-bearer-secret'}) "
                "Bearer bare-bearer-secret "
                'Bearer "quoted-bearer-secret" '
                "AWS_SECRET_ACCESS_KEY=aws-secret-456 "
                "https://alice:url-secret-789@example.com/path "
                "postgresql://dbuser:db-secret-321@db.example/app "
                "redis://:redis-secret-654@cache.example:6380/2 "
                "-----BEGIN PRIVATE KEY----- pem-secret-000 "
                "-----END PRIVATE KEY-----\n"
            )
        ),
        base_branch="main",
        branch="moa/ci-fix-aaaaaaaaaaaa",
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )

    artifacts = write_ci_triage_artifacts(report, tmp_path)
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            artifacts.json_path,
            artifacts.markdown_path,
            artifacts.request_path,
        )
    )

    for secret in secrets:
        assert secret not in combined


def test_classify_ci_failure_uses_deterministic_evidence() -> None:
    log = (
        "collected 12 items\n"
        "FAILED tests/test_api.py::test_create - AssertionError: expected 200\n"
        "short test summary info\n"
    )

    category, evidence = classify_ci_failure(log, conclusion="failure")

    assert category == "test_failure"
    assert evidence == (
        "FAILED tests/test_api.py::test_create - AssertionError: expected 200",
    )


def test_unknown_classification_does_not_invent_a_root_cause() -> None:
    category, evidence = classify_ci_failure("all output looks normal", conclusion="failure")

    assert category == "unknown"
    assert evidence == ()


def test_test_failure_wins_over_timeout_words_in_passing_test_names() -> None:
    log = (
        "tests/unit/test_config.py::test_router_llm_timeout_env PASSED [ 12%]\n"
        "tests/unit/test_sync_bridge.py::test_timeout_is_one_second PASSED [ 93%]\n"
        "2026-09-29T04:16:56.6314717Z FAILED "
        "tests/unit/test_github_webhook_e2e.py::test_command - assert 500 == 200\n"
        "2026-09-29T04:16:56.6315380Z  +  where 500 = "
        "<Response [500 Internal Server Error]>.status_code\n"
        "2026-09-29T04:16:56.6315792Z ========== 1 failed, 853 passed "
        "in 14.70s ==========\n"
        "2026-09-29T04:16:57.4842381Z ##[error]Process completed with exit code 1.\n"
    )

    category, evidence = classify_ci_failure(log, conclusion="failure")

    assert category == "test_failure"
    assert any("FAILED tests/unit/test_github_webhook_e2e.py" in line for line in evidence)
    assert any("1 failed, 853 passed" in line for line in evidence)
    assert all("PASSED" not in line for line in evidence)


def test_report_maps_to_ci_failure_delivery_request() -> None:
    report = build_ci_triage_report(
        _evidence(
            _job(
                "Traceback (most recent call last):\n"
                "ModuleNotFoundError: No module named 'httpx'\n"
            )
        ),
        base_branch="main",
        branch="moa/ci-fix-aaaaaaaaaaaa",
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        now=datetime(2026, 10, 28, 1, 2, 3, tzinfo=UTC),
    )

    request = report.to_delivery_request()

    assert request.origin == "ci_failure"
    assert request.base_sha == SHA
    assert request.source_ref == f"github-ci:owner/name@{SHA}"
    assert "untrusted" in request.goal
    assert "dependency" in request.body
    assert "ModuleNotFoundError" in request.body


def test_markdown_report_is_explicitly_read_only() -> None:
    report = build_ci_triage_report(
        _evidence(_job("error: could not resolve dependency\n")),
        base_branch="main",
        branch="moa/ci-fix-aaaaaaaaaaaa",
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )

    markdown = report.to_markdown()

    assert "Human review required" in markdown
    assert "No code was changed" in markdown
    assert "dependency" in markdown


def test_artifacts_include_report_and_delivery_request(tmp_path: Path) -> None:
    report = build_ci_triage_report(
        _evidence(_job("permission denied: resource not accessible\n")),
        base_branch="main",
        branch="moa/ci-fix-aaaaaaaaaaaa",
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )

    artifacts = write_ci_triage_artifacts(report, tmp_path)

    assert artifacts.json_path.is_file()
    assert artifacts.markdown_path.is_file()
    assert artifacts.request_path.is_file()
    payload = json.loads(artifacts.request_path.read_text(encoding="utf-8"))
    report_payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["origin"] == "ci_failure"
    assert payload["trace_id"] == "ci-owner-name-aaaaaaaaaaaa"
    assert report_payload["state"] == "failed"


def test_unavailable_artifacts_record_permission_failure_without_request(
    tmp_path: Path,
) -> None:
    from apps.code_review_pipeline.delivery import ci_triage as ci_triage_module

    artifacts = ci_triage_module.write_ci_triage_unavailable_artifacts(
        repo="owner/name",
        head_sha=SHA,
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        directory=tmp_path,
        status_code=403,
    )

    assert artifacts.json_path.is_file()
    assert artifacts.markdown_path.is_file()
    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert payload["failure_status_established"] is False
    assert payload["human_review_required"] is True
    assert "Checks: Read" in markdown
    assert "rate-limited" in markdown
    assert "does not mean CI is green" in markdown
    assert not list(tmp_path.glob("*.request.json"))


def test_unavailable_artifacts_invalidate_stale_delivery_request(
    tmp_path: Path,
) -> None:
    from apps.code_review_pipeline.delivery import ci_triage as ci_triage_module

    report = build_ci_triage_report(
        _evidence(_job("FAILED tests/test_api.py\n")),
        base_branch="main",
        branch="moa/ci-fix-aaaaaaaaaaaa",
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    written = write_ci_triage_artifacts(report, tmp_path)
    assert written.request_path.is_file()

    unavailable = ci_triage_module.write_ci_triage_unavailable_artifacts(
        repo="owner/name",
        head_sha=SHA,
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        directory=tmp_path,
        status_code=403,
    )

    assert unavailable.json_path == written.json_path
    assert not written.request_path.exists()


@pytest.mark.parametrize(
    ("status_code", "expected"),
    (
        (404, "not found"),
        (429, "rate limit"),
        (500, "upstream"),
    ),
)
def test_unavailable_artifacts_explain_non_permission_http_failure(
    tmp_path: Path,
    status_code: int,
    expected: str,
) -> None:
    from apps.code_review_pipeline.delivery import ci_triage as ci_triage_module

    artifacts = ci_triage_module.write_ci_triage_unavailable_artifacts(
        repo="owner/name",
        head_sha=SHA,
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        directory=tmp_path,
        status_code=status_code,
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")
    assert expected in markdown.lower()
    assert "Grant fine-grained GitHub" not in markdown


def test_truncated_report_does_not_claim_unseen_jobs_were_omitted() -> None:
    report = build_ci_triage_report(
        CiFailureEvidence(
            repo="owner/name",
            head_sha=SHA,
            checks=(),
            jobs=(_job("FAILED tests/test_api.py\n"),),
            jobs_truncated=True,
        ),
        base_branch="main",
        branch="moa/ci-fix-aaaaaaaaaaaa",
        trace_id="ci-owner-name-aaaaaaaaaaaa",
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )

    markdown = report.to_markdown()

    assert "configured job limit was reached" in markdown
    assert "Additional failed jobs were omitted" not in markdown
