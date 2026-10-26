from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    AgentAttempt,
    AgentStep,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    FailureCode,
    FailureInfo,
)


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        trace_id="trace-1",
        repo="owner/name",
        base_sha="a" * 40,
        base_branch="main",
        branch="moa/fix-1",
        goal="fix add",
        title="fix: correct add",
        body="body",
        origin="manual",
        source_ref="",
    )


def _usage() -> DeliveryUsage:
    return DeliveryUsage(
        llm_calls=2,
        prompt_tokens=30,
        completion_tokens=10,
        cost_usd=0.01,
        llm_latency_ms=12.5,
    )


def test_request_is_immutable_and_uses_the_schema_version() -> None:
    request = _request()
    with pytest.raises(FrozenInstanceError):
        request.branch = "other"  # type: ignore[misc]
    run = DeliveryRun(
        schema_version=SCHEMA_VERSION,
        run_id="run-1",
        trace_id=request.trace_id,
        request=request,
        topology="single",
        state=DeliveryRunState.PREPARING,
        attempts=(),
        verification=None,
        verifier_verdicts=(),
        bundle=None,
        push=None,
        merge=None,
        usage=DeliveryUsage(),
        failure=None,
        started_at=datetime(2026, 10, 27, tzinfo=UTC),
        updated_at=datetime(2026, 10, 27, tzinfo=UTC),
    )
    payload = run.to_dict()
    assert payload["schema_version"] == 1
    assert payload["state"] == "preparing"
    assert payload["request"]["repo"] == "owner/name"
    assert set(payload) == {
        "schema_version",
        "run_id",
        "trace_id",
        "request",
        "topology",
        "state",
        "attempts",
        "verification",
        "verifier_verdicts",
        "bundle",
        "push",
        "merge",
        "usage",
        "failure",
        "started_at",
        "updated_at",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repo": "name-only"}, "owner/name"),
        ({"trace_id": " "}, "trace_id"),
        ({"branch": ""}, "branch"),
        ({"base_sha": ""}, "base_sha"),
        ({"goal": ""}, "goal"),
    ],
)
def test_request_rejects_invalid_identity(kwargs: dict[str, str], message: str) -> None:
    values = {
        "trace_id": "trace-1",
        "repo": "owner/name",
        "base_sha": "a" * 40,
        "base_branch": "main",
        "branch": "moa/fix-1",
        "goal": "fix add",
        "title": "fix",
        "body": "",
        "origin": "manual",
        "source_ref": "",
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        DeliveryRequest(**values)  # type: ignore[arg-type]


def test_policy_defaults_and_fixed_revision_budget() -> None:
    policy = DeliveryPolicy(verify_command=("python", "-m", "pytest", "-q"))
    assert policy.topology == "single"
    assert policy.max_revisions == 1
    assert policy.max_steps == 40
    assert policy.max_tokens == 400_000
    with pytest.raises(ValueError, match="max_revisions"):
        DeliveryPolicy(verify_command=("pytest",), max_revisions=2)


def test_attempt_usage_and_failure_serialize_without_losing_evidence() -> None:
    attempt = AgentAttempt(
        role="implementer",
        status="completed",
        summary="fixed",
        stop_reason="model finished",
        usage=_usage(),
        steps=(
            AgentStep(
                index=1,
                action="call_tool",
                tool="write_file",
                arguments={"path": "app.py"},
                observation="written",
                outcome="ok",
                elapsed_ms=1.5,
            ),
        ),
    )
    payload = attempt.to_dict()
    assert payload["usage"]["llm_calls"] == 2
    assert payload["steps"][0]["tool"] == "write_file"
    assert FailureInfo(FailureCode.VERIFY_FAILED, "red").to_dict() == {
        "code": "verify_failed",
        "message": "red",
    }
