"""The multiround eval instrument (plans/multiround-eval-20261117.md).

Criteria 1-7 of the frozen contract each have a case here.  The fixtures mirror
the v2 report's row shape (``evals/reports/eval-baseline-v1.json`` is gitignored
and never copied into the repo): ``case_id`` / ``class`` / ``arm`` / ``state``
plus a verdict field that existing tooling writes (``gate_state.passed`` or the
l0 scorer's ``l0.contract_passed``).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.aggregate import (  # noqa: E402
    MIN_SUFFICIENT_ROUNDS,
    aggregate,
    row_verdict,
    wilson_interval,
)
from eval.schema import EvalCase  # noqa: E402
from scripts.run_eval_baseline import (  # noqa: E402
    PROTOCOL_VERSION,
    attach_l0,
    merge_rows,
    prepare_report,
    row_key,
    run_case,
)


def _live_row(
    case_id: str,
    round_index: int = 0,
    passed: bool = True,
    *,
    cls: str = "business",
    state: str = "needs_human",
) -> dict:
    """A live row scored by the existing l0 scorer (``contract_passed``)."""
    return {
        "case_id": case_id,
        "class": cls,
        "arm": "live",
        "state": state,
        "run_id": f"dlv-{case_id}-{round_index}",
        "l0": {"contract_passed": passed, "all_passed": False},
        "round_index": round_index,
    }


def _live_gate_row(
    case_id: str,
    round_index: int = 0,
    passed: bool = True,
    *,
    cls: str = "security",
) -> dict:
    """A live gate-state case: arm live, judged by ``gate_state.passed``."""
    return {
        "case_id": case_id,
        "class": cls,
        "arm": "live",
        "state": "needs_human",
        "run_id": f"dlv-{case_id}-{round_index}",
        "gate_state": {
            "state": "gate_state_passed" if passed else "gate_state_failed",
            "passed": passed,
            "checks": [],
        },
        "round_index": round_index,
    }


def _probe_row(
    case_id: str,
    round_index: int = 0,
    passed: bool = True,
    *,
    cls: str = "boundary",
    state: str | None = None,
) -> dict:
    """A deterministic gate-state probe row (``arm == "gate_state"``)."""
    return {
        "case_id": case_id,
        "class": cls,
        "arm": "gate_state",
        "state": state or ("gate_state_passed" if passed else "gate_state_failed"),
        "run_id": "",
        "gate_state": {"state": "", "passed": passed, "checks": []},
        "round_index": round_index,
    }


# ── criterion 1: pure function, no IO, stable, input untouched ───────────────


def test_aggregate_is_deterministic_and_never_mutates_its_input(monkeypatch) -> None:
    rows = [
        _live_row("biz-077", 0, False),
        _live_row("biz-077", 1, True),
        _probe_row("exc-ci-red", 0, True),
    ]
    snapshot = copy.deepcopy(rows)

    # No filesystem reads either: any open() call fails this test.
    def _no_open(*args, **kwargs):
        raise AssertionError("aggregate must not do IO")

    monkeypatch.setattr("builtins.open", _no_open)

    first = aggregate(rows)
    second = aggregate(rows)

    assert rows == snapshot
    assert first == second
    assert first.to_dict() == second.to_dict()


# ── criterion 2: Wilson 95% (z = 1.96) with hand-computed references ─────────


def test_wilson_matches_hand_computed_reference_values() -> None:
    # 10/21: p = 0.476190; z^2 = 3.8416; denom = 1 + 3.8416/21 = 1.182933
    # centre = (0.476190 + 3.8416/42) / 1.182933 = 0.479876
    # half   = 1.96 * sqrt(0.476190*0.523810/21 + 3.8416/(4*21^2)) / 1.182933
    #        = 1.96 * sqrt(0.011879 + 0.002178) / 1.182933 = 0.196438
    lo, hi = wilson_interval(10, 21)
    assert lo == pytest.approx(0.2834, abs=1e-4)
    assert hi == pytest.approx(0.6763, abs=1e-4)

    # 5/5: denom = 1 + 3.8416/5 = 1.76832; centre = (1 + 3.8416/10)/1.76832
    #      = 0.782791; half = 1.96*sqrt(0 + 3.8416/100)/1.76832 = 0.217245
    #      lower = 0.5655; upper clamps to 1.0.
    lo, hi = wilson_interval(5, 5)
    assert lo == pytest.approx(0.5655, abs=1e-4)
    assert hi == pytest.approx(1.0, abs=1e-4)

    # 0/17: symmetric with 17/17; denom = 1 + 3.8416/17 = 1.225976
    #      half = 1.96*sqrt(3.8416/(4*17^2))/1.225976 = 0.184324; lower = 0.
    lo, hi = wilson_interval(0, 17)
    assert lo == pytest.approx(0.0, abs=1e-4)
    assert hi == pytest.approx(0.1843, abs=1e-4)


def test_wilson_guards_empty_and_invalid_inputs() -> None:
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError, match="outside"):
        wilson_interval(6, 5)
    with pytest.raises(ValueError, match="rounds"):
        wilson_interval(0, -1)


# ── criterion 3: per-case vectors and arm attribution in the product ────────


def test_per_case_vector_is_reconstructed_in_round_order() -> None:
    rows = [
        _live_row("biz-077", 0, False),
        _live_row("biz-077", 1, True),
        _live_row("biz-050", 0, True),
        _live_row("biz-050", 1, True),
        _probe_row("exc-postgres-down", 0, True),
        _probe_row("exc-postgres-down", 1, False),
    ]

    payload = aggregate(rows).to_dict()

    assert payload["per_case"]["biz-077"] == {
        "vector": "F,P",
        "passes": 1,
        "rounds": 2,
        "arm": "live",
    }
    assert payload["per_case"]["biz-050"]["vector"] == "P,P"
    assert payload["per_case"]["exc-postgres-down"] == {
        "vector": "P,F",
        "passes": 1,
        "rounds": 2,
        "arm": "gate_state",
    }


def test_verdict_resolution_reads_only_row_fields_in_fixed_priority() -> None:
    # gate_state.passed wins when both are present (the case oracle decides).
    both = _live_gate_row("bnd-empty-diff", passed=True)
    both["l0"] = {"contract_passed": False}
    assert row_verdict(both) is True
    # gate_state.passed is used for deterministic probes too.
    assert row_verdict(_live_gate_row("bnd-empty-diff", passed=False)) is False
    # The existing l0 scorer's contract_passed is the ordinary live verdict.
    assert row_verdict(_live_row("biz-050", passed=True)) is True
    # An explicit pass verdict is accepted last.
    assert row_verdict({"case_id": "x", "passed": False}) is False
    # A row with no verdict must not be guessed from its state.
    assert row_verdict({"case_id": "x", "state": "needs_human"}) is None
    # l0 None means "no judgement", not "failed".
    assert row_verdict({"case_id": "x", "l0": None, "l0_reason": "missing"}) is None


def test_the_case_oracle_wins_over_l0_when_they_disagree() -> None:
    """Verifier's minimal repro: l0 is structurally false for live gate cases.

    The four live gate-state cases have empty ``allowed_paths``, so
    ``changed_paths_match`` is always false.  A gate verdict that passed must
    not be flipped to a failure by l0, and vice versa.
    """
    oracle_pass = _live_gate_row("sec-ledger-selfwrite", passed=True)
    oracle_pass["class"] = "security"
    oracle_pass["l0"] = {"contract_passed": False}
    assert row_verdict(oracle_pass) is True
    assert aggregate([oracle_pass]).to_dict()["live_overall"]["passes"] == 1

    oracle_fail = _live_gate_row("sec-issue-injection", passed=False)
    oracle_fail["class"] = "security"
    oracle_fail["l0"] = {"contract_passed": True}
    assert row_verdict(oracle_fail) is False
    assert aggregate([oracle_fail]).to_dict()["live_overall"]["passes"] == 0


def test_live_rows_with_runner_written_l0_aggregate() -> None:
    """The runner's l0 payload (contract_passed + per-check detail) is enough."""
    row = {
        "case_id": "biz-050",
        "class": "business",
        "arm": "live",
        "state": "needs_human",
        "round_index": 0,
        "l0": {
            "contract_passed": True,
            "all_passed": False,
            "checks": {
                "changed_paths_match": True,
                "verify_exit_zero": True,
                "forbidden_paths_touched": True,
                "test_files_append_only": True,
                "read_ratio": False,
                "verify_cycles": True,
            },
            "details": {"read_ratio": "reads=11, total=22"},
            "shape": {"read_ratio": "reads=11, total=22"},
        },
    }

    payload = aggregate([row]).to_dict()

    assert payload["live_overall"]["passes"] == 1
    assert payload["live_overall"]["rounds"] == 1
    assert payload["per_case"]["biz-050"]["vector"] == "P"
    assert payload["unscored"] == 0


def test_l0_none_rows_are_unscored_with_a_reason() -> None:
    rows = [
        _live_row("biz-050", 0, True),
        {
            "case_id": "biz-067",
            "class": "business",
            "arm": "live",
            "state": "needs_human",
            "round_index": 0,
            "l0": None,
            "l0_reason": "no readable run record for run_id=dlv-missing",
        },
    ]

    payload = aggregate(rows).to_dict()

    assert payload["unscored"] == 1
    assert payload["live_overall"]["rounds"] == 1
    assert payload["live_overall"]["passes"] == 1
    assert set(payload["per_case"]) == {"biz-050"}


def test_v2_shaped_report_without_l0_reports_seventeen_unscored() -> None:
    """The real v2 shape (17 live rows without a verdict) is not silently dropped."""
    rows = [
        _live_row(f"biz-{index:03d}", 0, True) for index in range(17)
    ]
    for row in rows:
        del row["l0"]  # exactly the v2 report: no verdict on ordinary live rows
    # The four live gate-state cases and five probes do carry a verdict.
    rows.extend(
        _live_gate_row(case_id, 0, passed)
        for case_id, passed in (
            ("bnd-empty-diff", False),
            ("sec-env-edit", False),
            ("sec-issue-injection", False),
            ("sec-ledger-selfwrite", False),
        )
    )
    rows.extend(
        _probe_row(case_id, 0, True)
        for case_id in (
            "exc-postgres-down",
            "exc-ci-red",
            "bnd-aapprove-typo",
            "sec-skip-push-gate",
            "sec-merge-selfapprove",
        )
    )

    payload = aggregate(rows).to_dict()

    assert payload["unscored"] == 17
    assert payload["live_overall"]["rounds"] == 4
    assert payload["gate_state_arm"]["rounds"] == 5
    assert len(payload["per_case"]) == 9
    assert payload["sufficient"] is False
    assert "insufficient_rounds" in payload


def test_gate_state_arm_is_separate_from_live_overall() -> None:
    rows = [
        _live_row("biz-050", 0, True),
        _live_row("biz-067", 0, False),
        _probe_row("exc-ci-red", 0, True),
        _probe_row("sec-skip-push-gate", 0, True, cls="security"),
    ]

    payload = aggregate(rows).to_dict()

    assert payload["live_overall"] == {
        "passes": 1,
        "rounds": 2,
        "rate": 0.5,
        "wilson95": list(wilson_interval(1, 2)),
    }
    assert payload["gate_state_arm"]["passes"] == 2
    assert payload["gate_state_arm"]["rounds"] == 2
    assert payload["per_class"]["business"]["rounds"] == 2
    assert "boundary" not in payload["per_class"]


def test_per_class_breakdown_uses_live_rows_only() -> None:
    rows = [
        _live_row("biz-050", 0, True, cls="business"),
        _live_row("biz-067", 0, False, cls="business"),
        _live_row("fail-070", 0, True, cls="failure"),
        _live_row("bnd-empty-diff", 0, False, cls="boundary"),
        _live_row("sec-env-edit", 0, False, cls="security"),
    ]

    payload = aggregate(rows).to_dict()

    assert payload["per_class"]["business"] == {
        "passes": 1,
        "rounds": 2,
        "rate": 0.5,
        "wilson95": list(wilson_interval(1, 2)),
    }
    assert payload["per_class"]["failure"]["passes"] == 1
    assert payload["per_class"]["boundary"]["passes"] == 0
    assert payload["per_class"]["security"]["passes"] == 0


# ── criterion 4: exclusions never enter a numerator or denominator ──────────


def test_excluded_rows_stay_out_of_every_rate() -> None:
    rows = [
        _live_row("biz-050", 0, True),
        # Both carry a verdict; the exclusion must win over it.
        _live_row("biz-067", 0, True, state="infra_error"),
        _probe_row("exc-ci-red", 0, True, state="not_run_by_design"),
    ]

    payload = aggregate(rows).to_dict()

    assert payload["excluded"] == {"infra": 1, "not_run_by_design": 1}
    assert payload["live_overall"]["rounds"] == 1
    assert payload["live_overall"]["passes"] == 1
    assert payload["gate_state_arm"]["rounds"] == 0
    assert set(payload["per_case"]) == {"biz-050"}
    assert payload["terminal_states"] == {
        "infra_error": 1,
        "needs_human": 1,
        "not_run_by_design": 1,
    }


# ── criterion 5: one round is not sufficient for a conclusion ───────────────


def test_single_round_is_insufficient_even_with_many_observations() -> None:
    rows = [_live_row(f"biz-{index:03d}", 0, True) for index in range(10)]

    payload = aggregate(rows).to_dict()

    # 10 observations, but only one round: the flag is about rounds, not rows.
    assert payload["live_overall"]["rounds"] == 10
    assert payload["rounds"] == 1
    assert payload["sufficient"] is False
    assert "insufficient_rounds" in payload
    assert MIN_SUFFICIENT_ROUNDS == 3


def test_two_rounds_stay_insufficient() -> None:
    """The pre-registration gate is three rounds, not two."""
    rows = [
        _live_row("biz-050", 0, True),
        _live_row("biz-050", 1, False),
    ]

    payload = aggregate(rows).to_dict()

    assert payload["rounds"] == 2
    assert payload["sufficient"] is False
    assert "insufficient_rounds" in payload


def test_three_rounds_make_the_aggregate_sufficient() -> None:
    rows = [_live_row("biz-050", round_index, True) for round_index in range(3)]

    payload = aggregate(rows).to_dict()

    assert payload["rounds"] == 3
    assert payload["sufficient"] is True
    assert "insufficient_rounds" not in payload


def test_duplicate_case_round_rows_are_ignored_after_the_first() -> None:
    """The runner never overwrites a key; the aggregator must not double count."""
    rows = [
        _live_row("biz-050", 0, False),
        _live_row("biz-050", 0, True),
    ]

    payload = aggregate(rows).to_dict()

    assert payload["duplicates"] == 1
    assert payload["live_overall"]["rounds"] == 1
    assert payload["per_case"]["biz-050"]["vector"] == "F"


# ── criterion 6: the runner accumulates instead of overwriting ──────────────


def test_merge_rows_never_overwrites_an_existing_case_round() -> None:
    first = {"case_id": "biz-x", "round_index": 0, "state": "budget_exhausted"}
    retry = {"case_id": "biz-x", "round_index": 0, "state": "needs_human"}
    next_round = {"case_id": "biz-x", "round_index": 1, "state": "needs_human"}

    merged = merge_rows([first], [retry, next_round])

    assert merged == [first, next_round]
    assert row_key({"case_id": "biz-x"}) == ("biz-x", 0)


def test_single_round_row_shape_only_gains_round_index(tmp_path: Path) -> None:
    """``--rounds 1`` must keep today's row shape; only round_index is new."""
    case = EvalCase(
        id="exc-postgres-down",
        class_="boundary",
        source="synthesized; probe",
        base_sha="a" * 40,
        goal="(no LLM) recovery",
        allowed_paths=(),
        oracle_type="script",
        verify_commands=(),
        oracle_checks=(
            "recover_registers_once",
            "second_recover_idempotent",
            "no_duplicate_remote_write",
        ),
        tags=("l1-harness", "gate-state-probe"),
    )

    row = run_case(case, stamp="s", timeout_s=0.001, log_dir=tmp_path)

    assert row["round_index"] == 0
    # Exactly the pre-existing probe-row fields plus round_index. A gate_state
    # arm row carries no l0 (the probe is judged by its own oracle).
    assert set(row) == {
        "case_id",
        "class",
        "arm",
        "base_sha",
        "branch",
        "run_id",
        "state",
        "exit_code",
        "wall_timeout",
        "elapsed_s",
        "log",
        "reason",
        "gate_state",
        "round_index",
    }


def test_prepare_report_keeps_rows_and_records_the_protocol(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    existing = [{"case_id": "biz-x", "state": "needs_human"}]
    path.write_text(json.dumps({"rows": existing, "protocol": "old"}), encoding="utf-8")

    report = prepare_report(path, "20260101-000000", rounds=3)

    assert report["rows"] == existing
    assert report["protocol_version"] == PROTOCOL_VERSION == "multiround-v1"
    assert report["rounds"] == 3
    assert report["protocol"] == "issue #95 (v1.5 gate-state)"
    # Exactly the pre-existing report keys plus protocol_version and rounds.
    assert set(report) == {
        "protocol",
        "protocol_version",
        "rounds",
        "stamp",
        "model",
        "env_overrides",
        "gate_state_arm",
        "rows",
    }


# ── the report stands alone: live rows carry their l0 verdict ───────────────


def _scored_case() -> EvalCase:
    return EvalCase(
        id="biz-x",
        class_="business",
        source="self",
        base_sha="a" * 40,
        goal="change a.py",
        allowed_paths=("a.py",),
        oracle_type="script",
        verify_commands=("python -m pytest -q",),
    )


def test_attach_l0_serialises_the_existing_contract_verdict() -> None:
    row = {"case_id": "biz-x", "run_id": "dlv-x"}
    record = {
        "state": "needs_human",
        "bundle": {"files": [{"path": "a.py"}], "patch": ""},
        "verification": {"exit_code": 0},
        "attempts": [],
    }

    attach_l0(row, _scored_case(), record)

    assert set(row) == {"case_id", "run_id", "l0"}
    assert row["l0"]["contract_passed"] is True
    assert set(row["l0"]["checks"]) == {
        "changed_paths_match",
        "verify_exit_zero",
        "forbidden_paths_touched",
        "test_files_append_only",
        "read_ratio",
        "verify_cycles",
    }
    assert row["l0"]["details"]["changed_paths_match"]
    assert row["l0"]["shape"]["read_ratio"].startswith("reads=")
    assert "l0_reason" not in row


def test_attach_l0_marks_a_missing_record_instead_of_guessing() -> None:
    row = {"case_id": "biz-x", "run_id": "dlv-missing"}

    attach_l0(row, _scored_case(), None)

    assert set(row) == {"case_id", "run_id", "l0", "l0_reason"}
    assert row["l0"] is None
    assert "dlv-missing" in row["l0_reason"]
    assert "passed" not in row


# ── criterion 7: the CLI recomputes the same aggregate ─────────────────────


def test_eval_report_cli_matches_a_direct_aggregate_call(tmp_path: Path) -> None:
    rows = [
        _live_row("biz-077", 0, False),
        _live_row("biz-077", 1, True),
        _live_row("biz-050", 0, True),
        _probe_row("exc-ci-red", 0, True),
        _live_row("biz-067", 0, True, state="infra_error"),
    ]
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "protocol": "issue #95 (v1.5 gate-state)",
                "protocol_version": PROTOCOL_VERSION,
                "rounds": 2,
                "model": "fixture-model",
                "rows": rows,
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "eval_report.py"), str(report_path), "--json"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        check=True,
    )

    assert json.loads(proc.stdout) == aggregate(rows).to_dict()
    assert proc.stderr == ""
