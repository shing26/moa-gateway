"""Reproducible summary of an eval baseline report (plans/multiround-eval-20261117.md).

    python scripts/eval_report.py evals/reports/<file>.json [--json]

Reads one report JSON file and prints the aggregate over its rows.  It never
reads the database, ``data/``, the model endpoints or any environment override:
a third party holding the report file can recompute every number.  No side
effects beyond stdout.

The pass verdict comes from each row (``gate_state.passed``, the existing
scorer's ``l0.contract_passed``, or an explicit ``passed``).  Rows without a
verdict are counted as ``unscored`` and stay out of every rate — the script
never turns a terminal state into a pass/fail judgement.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.aggregate import Aggregate, aggregate


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _interval(bounds: tuple[float, float] | None) -> str:
    if bounds is None:
        return "[n/a]"
    return f"[{bounds[0]:.4f}, {bounds[1]:.4f}]"


def _stats_line(name: str, stats, width: int = 12) -> str:
    return (
        f"{name:<{width}} {stats.passes:>3}/{stats.rounds:<3} "
        f"rate={_rate(stats.rate):>7} wilson95={_interval(stats.wilson95)}"
    )


def render(report: dict, result: Aggregate) -> str:
    lines: list[str] = []
    lines.append(f"protocol: {report.get('protocol', '-')}")
    lines.append(
        f"protocol_version: {report.get('protocol_version', '-')}  "
        f"model: {report.get('model', '-')}  "
        f"requested_rounds: {report.get('rounds', '-')}"
    )
    lines.append(f"rows: {len(report.get('rows') or [])}  observed_rounds: {result.rounds}")
    lines.append("")
    lines.append('live arm only (arm == "live"; excludes gate_state arm and exclusions)')
    for name, stats in result.per_class.items():
        lines.append(_stats_line(f"  {name}", stats, width=14))
    lines.append(_stats_line("  overall", result.live_overall, width=14))
    lines.append("")
    lines.append(_stats_line("gate_state arm", result.gate_state_arm, width=16))
    lines.append(
        "excluded: "
        + " ".join(f"{name}={count}" for name, count in result.excluded.items())
    )
    lines.append(
        "terminal states: "
        + (" ".join(f"{name}={count}" for name, count in result.terminal_states.items()) or "-")
    )
    if result.unscored:
        lines.append(
            f"unscored rows (no verdict in the row): {result.unscored} "
            "- not counted as pass or fail"
        )
    if result.duplicates:
        lines.append(f"duplicate (case_id, round_index) rows ignored: {result.duplicates}")
    lines.append("")
    lines.append("per-case vectors (round order):")
    for case_id, item in result.per_case.items():
        lines.append(
            f"  {case_id:<28} {item.arm:<10} {item.vector:<12} {item.passes}/{item.rounds}"
        )
    if not result.sufficient:
        lines.append("")
        lines.append(
            "insufficient_rounds: "
            + str(result.to_dict()["insufficient_rounds"])
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="path to the report JSON")
    parser.add_argument("--json", action="store_true", help="emit the aggregate as JSON")
    args = parser.parse_args(argv)

    path = Path(args.report)
    if not path.is_file():
        print(f"report not found: {path}", file=sys.stderr)
        return 2
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read report {path}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(report, dict) or not isinstance(report.get("rows"), list):
        print(f"not an eval report (missing 'rows' list): {path}", file=sys.stderr)
        return 2

    result = aggregate(report["rows"])
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
