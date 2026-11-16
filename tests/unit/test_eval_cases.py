"""Durability checks for the shipped eval case set (eval/cases/*.jsonl).

The case files are curated data, not code: every business/failure case replays a
real governed delivery run (goal text copied verbatim from that run's record),
and every boundary/security case is synthesized from a named real anchor. These
tests keep the shipped set loadable by `eval.schema.load_cases` and keep the
curation invariants that make a case replayable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eval.gate_state import PROBES, is_live_gate_case, is_probe_case
from eval.schema import EvalCase, load_cases

CASES_DIR = Path(__file__).resolve().parents[2] / "eval" / "cases"

EXPECTED_COUNTS = {"business": 8, "failure": 8, "boundary": 5, "security": 5}

ID_PREFIXES = {
    "business": ("biz-",),
    "failure": ("fail-",),
    "boundary": ("bnd-", "exc-"),
    "security": ("sec-",),
}

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DLV_RE = re.compile(r"dlv-[0-9a-f]{32}")

#: The nine cases the baseline v1 report recorded as not_run_by_design. Four
#: are model-behaviour cases (live + gate verdict); five are gate mechanisms
#: measured directly by eval/gate_state.py.
LIVE_GATE_IDS = {
    "bnd-empty-diff",
    "sec-env-edit",
    "sec-ledger-selfwrite",
    "sec-issue-injection",
}
PROBE_IDS = {
    "exc-postgres-down",
    "exc-ci-red",
    "bnd-aapprove-typo",
    "sec-skip-push-gate",
    "sec-merge-selfapprove",
}


def _case_paths() -> list[Path]:
    return sorted(CASES_DIR.glob("*.jsonl"))


def _all_cases() -> dict[str, EvalCase]:
    return {
        case.id: case
        for path in _case_paths()
        for case in load_cases(str(path))
    }


@pytest.mark.parametrize("case_class", sorted(EXPECTED_COUNTS))
def test_case_file_matches_its_class_and_count(case_class: str) -> None:
    path = CASES_DIR / f"{case_class}.jsonl"
    assert path.is_file(), f"missing case file: {path}"

    cases = load_cases(str(path))

    assert len(cases) == EXPECTED_COUNTS[case_class]
    assert {case.class_ for case in cases} == {case_class}


def test_case_ids_are_unique_across_the_whole_set() -> None:
    seen: dict[str, str] = {}
    for path in _case_paths():
        for case in load_cases(str(path)):
            assert case.id not in seen, f"{case.id} appears in {seen[case.id]} and {path.name}"
            seen[case.id] = path.name


@pytest.mark.parametrize("path", _case_paths(), ids=lambda p: p.name)
def test_every_case_is_replayable(path: Path) -> None:
    case_class = path.stem
    for case in load_cases(str(path)):
        assert case.id.startswith(ID_PREFIXES[case_class]), case.id
        assert _SHA_RE.match(case.base_sha), f"{case.id}: base_sha={case.base_sha!r}"
        assert case.goal.strip(), f"{case.id}: empty goal"

        if case_class in ("business", "failure"):
            # A replayable delivery case needs a write scope and a real gate.
            assert case.allowed_paths, f"{case.id}: no allowed_paths"
            assert case.verify_commands, f"{case.id}: no verify_commands"
            assert _DLV_RE.search(case.source), f"{case.id}: source names no run record"
        else:
            # Synthesized cases assert on gate state, so they must name the
            # checks the oracle runs.
            assert case.oracle_checks, f"{case.id}: no oracle_checks"
            assert case.source.strip(), f"{case.id}: no anchor in source"


def test_the_nine_formerly_deferred_cases_have_a_measurable_path() -> None:
    """No boundary/security case may be unreachable: CLI exit 2 is not a path."""
    cases = _all_cases()
    for case_id in LIVE_GATE_IDS | PROBE_IDS:
        assert case_id in cases, f"{case_id} disappeared from the corpus"
    for case_id, case in cases.items():
        if case.class_ not in ("boundary", "security"):
            continue
        assert case.verify_commands or is_probe_case(case), (
            f"{case_id}: neither a declared verify command nor a gate-state probe"
        )


def test_live_and_probe_tags_cover_exactly_the_nine_cases() -> None:
    cases = _all_cases()
    live = {case_id for case_id, case in cases.items() if is_live_gate_case(case)}
    probe = {case_id for case_id, case in cases.items() if is_probe_case(case)}

    assert live == LIVE_GATE_IDS
    assert probe == PROBE_IDS
    assert set(PROBES) == PROBE_IDS
    for case_id in live:
        assert cases[case_id].verify_commands, f"{case_id}: live gate needs a verify command"
        assert case_id not in PROBES, f"{case_id}: a live case must not also be a probe"
    for case_id in probe:
        assert not cases[case_id].verify_commands, f"{case_id}: probe must declare no verify command"
        assert cases[case_id].oracle_checks, f"{case_id}: probe needs oracle_checks"


def test_every_live_case_declares_the_neutral_verify_command() -> None:
    """The four live gate cases declare it in the corpus; the runner injects nothing."""
    cases = _all_cases()
    for case_id in LIVE_GATE_IDS:
        assert cases[case_id].verify_commands == (
            "python -c \"print('gate-state-declared-verify')\"",
        ), case_id
        # The injection payload says `python -c pass`; sharing it would make
        # "not neutered" indistinguishable from "declared that way".
        assert cases[case_id].verify_commands[0] != "python -c pass"
