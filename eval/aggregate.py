"""Pure aggregation of eval baseline report rows (plans/multiround-eval-20261117.md).

``aggregate(rows)`` is a pure function: no environment, no files, no network, no
clock.  The same rows always produce the same :class:`Aggregate`; a third party
holding only the report JSON can recompute every number.

Verdict resolution reads only fields inside the row, in this fixed order:

1. ``row["l0"]["contract_passed"]`` — ``eval.l0.L0Report.contract_passed``; the
   runner scores every live row with the existing ``eval.l0.check_run`` and
   writes the full report into the row, so the report file stands alone;
2. ``row["gate_state"]["passed"]`` — ``eval.gate_state.GateVerdict.passed``
   (always present on ``arm == "gate_state"`` rows);
3. an explicit ``row["passed"]``;

a row with none of those verdicts is **unscored**: it is counted in
``terminal_states`` (and ``unscored``) but never in a numerator or denominator.
A row whose ``l0`` is ``None`` carries ``l0_reason`` and is not guessed from
``state``.  The aggregator never scores a run itself and never reads anything
outside its argument.

Exclusions are by terminal state: ``infra_error`` -> ``excluded["infra"]`` and
``not_run_by_design`` -> ``excluded["not_run_by_design"]``.  They never enter a
rate.  ``arm == "gate_state"`` rows are kept out of ``live_overall`` and
``per_class`` and reported separately.

Interval method is frozen to Wilson 95% (z = 1.96): it does not collapse to a
zero-width interval when p-hat is near 0 or 1, which is the shape of the current
data (10/21, 5/5 probes).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: Frozen by the contract: Wilson score interval at 95%, z = 1.96.
Z_95 = 1.96

#: A pass rate needs at least two rounds; one round is a single coin flip.
MIN_SUFFICIENT_ROUNDS = 2

CLASS_ORDER = ("business", "failure", "boundary", "security")

#: Terminal states that are not measurements of the model or the gate.
_EXCLUDED_STATES = {
    "infra_error": "infra",
    "not_run_by_design": "not_run_by_design",
}

_KNOWN_ARMS = frozenset({"live", "gate_state"})


def wilson_interval(
    passes: int, rounds: int, *, z: float = Z_95
) -> tuple[float, float] | None:
    """Wilson score interval for ``passes / rounds``; ``None`` when rounds == 0.

    ``centre = (p + z^2/2n) / (1 + z^2/n)``
    ``half   = z * sqrt(p(1-p)/n + z^2/4n^2) / (1 + z^2/n)``
    """
    if rounds < 0:
        raise ValueError(f"rounds must be >= 0, got {rounds}")
    if passes < 0 or passes > rounds:
        raise ValueError(f"passes={passes} outside [0, rounds={rounds}]")
    if rounds == 0:
        return None
    p = passes / rounds
    z2 = z * z
    denominator = 1.0 + z2 / rounds
    centre = (p + z2 / (2 * rounds)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / rounds + z2 / (4.0 * rounds * rounds)) / denominator
    # The raw formula can overshoot by float epsilon at p == 0 or p == 1.
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class RateStats:
    """A pass count over rounds, with its rate and Wilson interval."""

    passes: int
    rounds: int
    rate: float | None
    wilson95: tuple[float, float] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passes": self.passes,
            "rounds": self.rounds,
            "rate": self.rate,
            "wilson95": list(self.wilson95) if self.wilson95 is not None else None,
        }


def rate_stats(passes: int, rounds: int) -> RateStats:
    return RateStats(
        passes=passes,
        rounds=rounds,
        rate=(passes / rounds) if rounds else None,
        wilson95=wilson_interval(passes, rounds),
    )


@dataclass(frozen=True)
class CaseVector:
    """One case's per-round verdicts, in round order: ``F,P`` etc."""

    vector: str
    passes: int
    rounds: int
    arm: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "vector": self.vector,
            "passes": self.passes,
            "rounds": self.rounds,
            "arm": self.arm,
        }


@dataclass(frozen=True)
class Aggregate:
    per_case: dict[str, CaseVector]
    live_overall: RateStats
    per_class: dict[str, RateStats]
    gate_state_arm: RateStats
    excluded: dict[str, int]
    terminal_states: dict[str, int]
    rounds: int
    duplicates: int
    unscored: int
    sufficient: bool

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "per_case": {case_id: item.to_dict() for case_id, item in self.per_case.items()},
            "live_overall": self.live_overall.to_dict(),
            "per_class": {name: item.to_dict() for name, item in self.per_class.items()},
            "gate_state_arm": self.gate_state_arm.to_dict(),
            "excluded": dict(self.excluded),
            "terminal_states": dict(self.terminal_states),
            "rounds": self.rounds,
            "duplicates": self.duplicates,
            "unscored": self.unscored,
            "sufficient": self.sufficient,
        }
        if not self.sufficient:
            payload["insufficient_rounds"] = (
                f"{self.rounds} round(s) observed; a pass rate needs at least "
                f"{MIN_SUFFICIENT_ROUNDS}. Two single-round sessions already flipped "
                "6 of 17 verdicts (docs/acceptance/eval-baseline-v2.md, section 2), so these "
                "numbers are not a conclusion."
            )
        return payload


def _round_index(row: Mapping[str, Any]) -> int:
    value = row.get("round_index", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid round_index: {value!r}")
    return value


def _row_key(row: Mapping[str, Any]) -> tuple[str, int]:
    case_id = row.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"row has no case_id: {row!r}")
    return (case_id, _round_index(row))


def row_verdict(row: Mapping[str, Any]) -> bool | None:
    """The row's pass verdict, from fields inside the report row only.

    Priority is fixed: ``l0.contract_passed`` -> ``gate_state.passed`` ->
    explicit ``passed``.  ``None`` means the row carries no judgement; callers
    must not guess one.
    """
    l0 = row.get("l0")
    if isinstance(l0, dict) and isinstance(l0.get("contract_passed"), bool):
        return l0["contract_passed"]
    gate = row.get("gate_state")
    if isinstance(gate, dict) and isinstance(gate.get("passed"), bool):
        return gate["passed"]
    direct = row.get("passed")
    if isinstance(direct, bool):
        return direct
    return None


def _ordered_classes(names: set[str]) -> list[str]:
    known = [name for name in CLASS_ORDER if name in names]
    return known + sorted(names - set(CLASS_ORDER))


def aggregate(rows: Sequence[Mapping[str, Any]]) -> Aggregate:
    """Aggregate report rows (pure; the input is never mutated).

    Duplicate ``(case_id, round_index)`` keys are counted in ``duplicates`` and
    dropped after the first occurrence, matching the runner's cumulative merge:
    an existing key is never overwritten.
    """
    seen: set[tuple[str, int]] = set()
    unique: list[Mapping[str, Any]] = []
    duplicates = 0
    for row in rows:
        key = _row_key(row)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(row)

    terminal_states: Counter[str] = Counter()
    excluded = {"infra": 0, "not_run_by_design": 0}
    unscored = 0
    rounds_seen: set[int] = set()

    # case_id -> list of (round_index, passed, arm)
    vectors: dict[str, list[tuple[int, bool, str]]] = {}
    live_passes = 0
    live_rounds = 0
    class_counts: dict[str, list[int]] = {}
    gate_passes = 0
    gate_rounds = 0

    for row in unique:
        round_index = _round_index(row)
        rounds_seen.add(round_index)
        state = str(row.get("state") or "")
        terminal_states[state or "(missing)"] += 1

        exclusion = _EXCLUDED_STATES.get(state)
        if exclusion is not None:
            excluded[exclusion] += 1
            continue

        arm = row.get("arm")
        if arm not in _KNOWN_ARMS:
            raise ValueError(f"{_row_key(row)[0]}: unknown arm {arm!r}")

        passed = row_verdict(row)
        if passed is None:
            unscored += 1
            continue

        case_id, _ = _row_key(row)
        vectors.setdefault(case_id, []).append((round_index, passed, arm))

        if arm == "gate_state":
            gate_rounds += 1
            gate_passes += int(passed)
        else:
            live_rounds += 1
            live_passes += int(passed)
            case_class = str(row.get("class") or "unknown")
            bucket = class_counts.setdefault(case_class, [0, 0])
            bucket[0] += int(passed)
            bucket[1] += 1

    per_case: dict[str, CaseVector] = {}
    for case_id, entries in vectors.items():
        entries.sort(key=lambda item: item[0])
        arms = {entry[2] for entry in entries}
        if len(arms) != 1:
            raise ValueError(f"{case_id}: mixed arms across rounds: {sorted(arms)}")
        per_case[case_id] = CaseVector(
            vector=",".join("P" if passed else "F" for _, passed, _ in entries),
            passes=sum(1 for _, passed, _ in entries if passed),
            rounds=len(entries),
            arm=entries[0][2],
        )

    per_class = {
        name: rate_stats(*class_counts[name]) for name in _ordered_classes(set(class_counts))
    }

    rounds = len(rounds_seen)
    return Aggregate(
        per_case=per_case,
        live_overall=rate_stats(live_passes, live_rounds),
        per_class=per_class,
        gate_state_arm=rate_stats(gate_passes, gate_rounds),
        excluded=excluded,
        terminal_states=dict(sorted(terminal_states.items())),
        rounds=rounds,
        duplicates=duplicates,
        unscored=unscored,
        sufficient=rounds >= MIN_SUFFICIENT_ROUNDS,
    )


__all__ = [
    "Aggregate",
    "CaseVector",
    "CLASS_ORDER",
    "MIN_SUFFICIENT_ROUNDS",
    "RateStats",
    "Z_95",
    "aggregate",
    "rate_stats",
    "row_verdict",
    "wilson_interval",
]
