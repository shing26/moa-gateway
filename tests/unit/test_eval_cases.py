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

from eval.schema import load_cases

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


def _case_paths() -> list[Path]:
    return sorted(CASES_DIR.glob("*.jsonl"))


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
