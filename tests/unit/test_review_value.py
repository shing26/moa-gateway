from __future__ import annotations

from dataclasses import replace

import pytest

from evals.review_value import (
    ReviewRunRecord,
    controlled_review_input,
    evaluate_runs,
    load_controlled_cases,
    load_expected_case_shas,
    load_pr_corpus,
    recommend_single_improvement,
)


_PROTOCOL = {
    "model_id": "test-model",
    "temperature": 0.0,
    "token_budget": 4096,
    "workspace_budget_seconds": 300,
    "base_sha": "a" * 40,
    "head_sha": "b" * 40,
}


def test_review_corpus_is_locked_and_balanced() -> None:
    rows = load_pr_corpus()
    assert len(rows) == 24
    assert {row["repo"] for row in rows} == {
        "fastapi/fastapi",
        "pydantic/pydantic",
        "sqlalchemy/sqlalchemy",
        "redis/redis-py",
    }
    assert all(row["base_sha"] and row["head_sha"] for row in rows)


def test_controlled_recall_set_has_twelve_single_defect_cases() -> None:
    cases = load_controlled_cases()
    assert len(cases) == 12
    assert sum(case.source == "E1" for case in cases) == 5
    assert sum(case.source == "E3" for case in cases) == 7
    assert all(case.known_defect_count == 1 for case in cases)


def test_controlled_review_input_redacts_tests_and_task_answers() -> None:
    case = next(
        item
        for item in load_controlled_cases()
        if item.case_id == "e3-c4-report-evidence-roundtrip"
    )

    review_input = controlled_review_input(case)

    assert "tests/unit/test_e3_report_evidence.py" not in review_input["diff"]
    assert "known_defect" not in review_input
    assert "note" not in review_input
    assert "verify_command" not in review_input
    assert "apps/code_review_pipeline/agents/report_agent.py" in review_input["diff"]


def _runs() -> tuple[ReviewRunRecord, ...]:
    rows = []
    for arm in ("R0", "R1", "R1+I"):
        rows.append(
            ReviewRunRecord(
                case_id=f"case-{arm}",
                arm=arm,
                findings=(
                    {
                        "finding_uid": f"{arm}-f1",
                        "disposition": "accepted_actionable",
                        "evidence": ["diff"],
                        "evidence_verified": True,
                        "published": True,
                        "critic_decision": "keep",
                        "label_source": "human",
                    },
                    {
                        "finding_uid": f"{arm}-f2",
                        "disposition": "false_positive",
                        "reason_code": "incorrect",
                        "evidence": [],
                        "published": True,
                        "critic_decision": "drop",
                        "label_source": "human",
                    },
                ),
                known_defects_found=(f"defect-{arm}",),
                known_defects_total=1,
                cost_usd=0.1,
                critic_cost_usd=0.01 if arm != "R0" else 0.0,
                latency_ms=1000,
                improvement_code=(
                    "evidence_gate" if arm == "R1+I" else ""
                ),
                changed_paths=(
                    ("apps/code_review_pipeline/agents/code_review_pipeline.py",)
                    if arm == "R1+I"
                    else ()
                ),
                **_PROTOCOL,
            )
        )
    return tuple(rows)


def test_metrics_keep_human_and_model_labels_separate() -> None:
    rows = list(_runs())
    rows[1] = replace(
        rows[1],
        findings=(
            {
                **rows[1].findings[0],
                "label_source": "model_prelabel",
            },
            rows[1].findings[1],
        ),
    )
    report = evaluate_runs(rows)
    r1 = report["arms"]["R1"]
    assert r1["labelled_findings"] == 1
    assert r1["factual_precision"] == 0.0
    assert report["arms"]["R0"]["factual_precision"] == 0.5


def test_single_improvement_uses_first_matching_rule() -> None:
    arms = {
        "R0": {},
        "R1": {
            "controlled_recall": 0.0,
            "factual_precision": 0.0,
            "duplicate_or_already_known_rate": 0.0,
            "accepted_non_actionable_rate": 0.0,
        },
        "R1+I": {},
    }
    assert recommend_single_improvement(arms)["code"] == "recall_rule_or_rag"


def test_evaluate_requires_all_three_arms() -> None:
    with pytest.raises(ValueError, match="R0, R1, and R1\\+I"):
        evaluate_runs((ReviewRunRecord(case_id="x", arm="R0", findings=()),))


def test_evaluate_rejects_missing_required_case_in_one_arm() -> None:
    rows = [
        ReviewRunRecord(
            case_id="required",
            arm=arm,
            findings=(),
            known_defects_total=1,
            improvement_code="recall_rule_or_rag" if arm == "R1+I" else "",
            changed_paths=(
                ("apps/code_review_pipeline/agents/code_review_pipeline.py",)
                if arm == "R1+I"
                else ()
            ),
        )
        for arm in ("R0", "R1")
    ]

    with pytest.raises(ValueError, match="R1\\+I case set drifted"):
        evaluate_runs(
            rows,
            controlled_case_ids={"required"},
            required_case_ids={"required"},
        )


def test_evaluate_rejects_drifted_controlled_denominator() -> None:
    rows = [
        ReviewRunRecord(
            case_id="required",
            arm=arm,
            findings=(),
            known_defects_total=2 if arm == "R1" else 1,
        )
        for arm in ("R0", "R1", "R1+I")
    ]

    with pytest.raises(ValueError, match="exactly one known defect"):
        evaluate_runs(rows, controlled_case_ids={"required"})


def test_evaluate_rejects_protocol_drift_between_arms() -> None:
    rows = list(_runs())
    rows[1] = replace(rows[1], token_budget=8192)

    with pytest.raises(ValueError, match="protocol drifted.*token_budget"):
        evaluate_runs(rows)


def test_manifest_sha_binding_rejects_consistent_but_unlisted_commit() -> None:
    rows = list(_runs())
    expected = {
        row.case_id: (row.base_sha, row.head_sha)
        for row in rows
    }
    evaluate_runs(rows, expected_case_shas=expected)

    tampered = [
        replace(row, head_sha="c" * 40)
        for row in rows
    ]
    with pytest.raises(ValueError, match="does not match manifest"):
        evaluate_runs(tampered, expected_case_shas=expected)


def test_manifest_loader_covers_real_and_controlled_cases() -> None:
    expected = load_expected_case_shas()
    assert len(expected) == 36
    assert all(
        len(base_sha) == 40 and len(head_sha) == 40
        for base_sha, head_sha in expected.values()
    )


def test_evaluation_requires_reason_for_non_actionable_labels() -> None:
    with pytest.raises(ValueError, match="reason_code is required"):
        ReviewRunRecord(
            case_id="x",
            arm="R0",
            findings=(
                {
                    "disposition": "false_positive",
                    "reason_code": "",
                },
            ),
        )


def test_empty_disposition_cannot_be_misrepresented_as_human() -> None:
    with pytest.raises(ValueError, match="unsupported finding label_source"):
        ReviewRunRecord(
            case_id="x",
            arm="R0",
            findings=(
                {
                    "disposition": "",
                    "label_source": "human",
                },
            ),
        )


def test_revision_and_drop_rates_use_unique_candidates() -> None:
    rows = list(_runs())
    rows[1] = replace(
        rows[1],
        findings=(
            {
                "finding_uid": "candidate-1",
                "status": "candidate",
                "published": False,
                "disposition": "",
                "label_source": "",
            },
            {
                "finding_uid": "candidate-1",
                "status": "dropped",
                "published": False,
                "critic_decision": "drop",
                "disposition": "",
                "label_source": "",
            },
            {
                "finding_uid": "candidate-2",
                "status": "candidate",
                "published": False,
                "disposition": "",
                "label_source": "",
            },
            {
                "finding_uid": "revised-2",
                "status": "published",
                "published": True,
                "revision_of": "candidate-2",
                "critic_decision": "revise",
                "evidence_verified": True,
                "disposition": "accepted_actionable",
                "label_source": "human",
            },
        ),
    )
    rows[-1] = replace(
        rows[-1],
        improvement_code="evidence_specificity",
    )

    r1 = evaluate_runs(rows)["arms"]["R1"]

    assert r1["revision_rate"] == 0.5
    assert r1["critic_drop_rate"] == 0.5


def test_r1_plus_i_improvement_must_match_predeclared_selection() -> None:
    rows = list(_runs())
    rows[-1] = replace(
        rows[-1],
        improvement_code="recall_rule_or_rag",
    )
    with pytest.raises(ValueError, match="does not match predeclared selection"):
        evaluate_runs(rows)


def test_evidence_coverage_requires_verified_flag_not_string_shape() -> None:
    rows = list(_runs())
    report = evaluate_runs(rows)
    assert report["arms"]["R0"]["evidence_coverage"] == 0.5

    rows[0] = replace(
        rows[0],
        findings=(
            {
                **rows[0].findings[0],
                "evidence": ["diff:missing.py:1"],
                "evidence_verified": False,
            },
            rows[0].findings[1],
        ),
    )
    report = evaluate_runs(rows)
    assert report["arms"]["R0"]["evidence_coverage"] == 0.0
