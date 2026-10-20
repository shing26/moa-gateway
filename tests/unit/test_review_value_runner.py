from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.review_value import ReviewRunRecord
from evals.review_value_runner import (
    MeteredLLM,
    _apply_model_prelabelled_rows,
    _persisted_findings,
    _validate_fixed_model_usage,
    execute_matrix,
    write_run_records,
)


@pytest.mark.asyncio
async def test_execute_matrix_uses_locked_shas_for_each_arm() -> None:
    calls: list[tuple[str, str, str, str]] = []

    async def _run_case(
        arm: str,
        case_id: str,
        base_sha: str,
        head_sha: str,
        improvement_code: str,
        changed_path: str,
    ) -> ReviewRunRecord:
        calls.append((arm, case_id, base_sha, head_sha))
        return ReviewRunRecord(
            case_id=case_id,
            arm=arm,
            findings=(),
            model_id="fixed-model",
            temperature=0.0,
            token_budget=12000,
            workspace_budget_seconds=300,
            base_sha=base_sha,
            head_sha=head_sha,
        )

    records = await execute_matrix(
        arms=("R0", "R1"),
        case_ids=("fastapi-16535",),
        run_case=_run_case,
    )

    assert [row.arm for row in records] == ["R0", "R1"]
    assert calls == [
        (
            "R0",
            "fastapi-16535",
            "2e13450ea624cafd69a528747d3b7272f2ab1c3d",
            "d937d9161da3744fc4fee18fb1a44398b39cd848",
        ),
        (
            "R1",
            "fastapi-16535",
            "2e13450ea624cafd69a528747d3b7272f2ab1c3d",
            "d937d9161da3744fc4fee18fb1a44398b39cd848",
        ),
    ]


@pytest.mark.asyncio
async def test_r1_plus_i_requires_a_single_predeclared_improvement() -> None:
    with pytest.raises(ValueError, match="improvement code"):
        await execute_matrix(
            arms=("R1+I",),
            case_ids=("fastapi-16535",),
        )


@pytest.mark.asyncio
async def test_metered_llm_accumulates_all_calls() -> None:
    class _Inner:
        def __init__(self) -> None:
            self.last_metrics: dict = {}

        async def chat(self, _messages, **_kwargs):
            self.last_metrics = {
                "model_used": "fixed-model",
                "cost_usd": 0.01,
                "llm_latency_ms": 10,
                "prompt_tokens": 100,
                "completion_tokens": 20,
            }
            return "ok"

        async def chat_with_tools(self, _messages, _tools, **_kwargs):
            self.last_metrics = {
                "model_used": "fixed-model",
                "cost_usd": 0.02,
                "llm_latency_ms": 20,
                "prompt_tokens": 200,
                "completion_tokens": 40,
            }
            return "tool-ok"

    llm = MeteredLLM(_Inner())
    await llm.chat([])
    await llm.chat_with_tools([], [])

    assert llm.usage == {
        "calls": 2,
        "cost_usd": pytest.approx(0.03),
        "latency_ms": pytest.approx(30.0),
        "prompt_tokens": 300,
        "completion_tokens": 60,
        "models_used": ["fixed-model"],
    }


@pytest.mark.asyncio
async def test_metered_llm_fails_immediately_after_token_budget_is_exceeded() -> None:
    class _Inner:
        def __init__(self) -> None:
            self.last_metrics: dict = {}

        async def chat(self, _messages, **_kwargs):
            self.last_metrics = {
                "model_used": "fixed-model",
                "prompt_tokens": 8,
                "completion_tokens": 3,
            }
            return "ok"

    llm = MeteredLLM(_Inner(), token_budget=10)

    with pytest.raises(RuntimeError, match="token budget exceeded"):
        await llm.chat([])


def test_model_drift_rejects_fallback_models() -> None:
    _validate_fixed_model_usage("fixed-model", ["fixed-model"])

    with pytest.raises(RuntimeError, match="fallback is not allowed"):
        _validate_fixed_model_usage("fixed-model", ["fixed-model", "fallback-model"])


@pytest.mark.asyncio
async def test_model_prelabel_stays_out_of_human_fields() -> None:
    class _LLM:
        async def chat(self, _messages, **_kwargs):
            return json.dumps(
                {
                    "disposition": "accepted_actionable",
                    "reason_code": "",
                    "rationale": "The diff supports the finding.",
                }
            )

    rows = await _apply_model_prelabelled_rows(
        (
            {
                "finding_uid": "f1",
                "published": True,
                "disposition": "",
                "reason_code": "",
                "label_source": "",
            },
            {
                "finding_uid": "dropped",
                "published": False,
                "disposition": "",
                "reason_code": "",
                "label_source": "",
            },
        ),
        llm=_LLM(),
        model_id="grader-model",
        diff="+ guard = True",
    )

    assert rows[0]["disposition"] == ""
    assert rows[0]["label_source"] == ""
    assert rows[0]["model_prelabel"]["disposition"] == "accepted_actionable"
    assert rows[0]["model_prelabel"]["model_id"] == "grader-model"
    assert rows[1]["model_prelabel"] is None


@pytest.mark.asyncio
async def test_model_prelabel_rejects_grader_model_drift() -> None:
    class _LLM:
        last_metrics = {"model_used": "fallback-grader"}

        async def chat(self, _messages, **_kwargs):
            return json.dumps(
                {
                    "disposition": "accepted_actionable",
                    "reason_code": "",
                }
            )

    with pytest.raises(RuntimeError, match="model prelabel drift"):
        await _apply_model_prelabelled_rows(
            ({"finding_uid": "f1", "published": True},),
            llm=_LLM(),
            model_id="grader-model",
            diff="+ guard = True",
        )


def test_persisted_findings_export_candidate_and_published_lineage() -> None:
    common = {
        "finding_uid": "f1",
        "fingerprint": "fingerprint",
        "trace_id": "trace",
        "agent": "report",
        "raw_finding_id": "raw-1",
        "revision_of": None,
        "severity": "high",
        "category": "logic",
        "file": "app/service.py",
        "line": 10,
        "title": "Missing guard",
        "description": "The value is not checked.",
        "suggestion": "Add a guard.",
        "confidence": 0.9,
        "team_specific": False,
        "evidence": ("diff:app/service.py:10",),
        "source_agents": ("semantic_review", "report"),
        "critic_decision": "keep",
        "critic_reason": "Diff supports it.",
        "evidence_verified": True,
    }
    result = SimpleNamespace(
        persisted_findings=(
            SimpleNamespace(**common, status="candidate"),
            SimpleNamespace(**common, status="published"),
        )
    )

    rows = _persisted_findings(result)

    assert [row["status"] for row in rows] == ["candidate", "published"]
    assert [row["published"] for row in rows] == [False, True]
    assert all(row["agent"] == "report" for row in rows)
    assert rows[0]["disposition"] == ""
    assert rows[0]["label_source"] == ""


def test_write_run_records_merges_without_duplicate_arm_case(tmp_path: Path) -> None:
    path = tmp_path / "runs.json"
    first = ReviewRunRecord(
        case_id="case",
        arm="R0",
        findings=(),
        model_id="fixed-model",
        temperature=0.0,
        token_budget=12000,
        workspace_budget_seconds=300,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    replacement = ReviewRunRecord(
        case_id="case",
        arm="R0",
        findings=(),
        cost_usd=0.2,
        model_id="fixed-model",
        temperature=0.0,
        token_budget=12000,
        workspace_budget_seconds=300,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    write_run_records(path, (first,))
    write_run_records(path, (replacement,))

    rows = json.loads(path.read_text(encoding="utf-8"))["runs"]
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == 0.2
