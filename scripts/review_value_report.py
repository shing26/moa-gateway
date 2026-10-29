"""Render review value-loop metrics from exported R0/R1/R1+I run records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.review_value import (
    ReviewRunRecord,
    evaluate_runs,
    load_controlled_cases,
    load_expected_case_shas,
    load_pr_corpus,
    render_markdown,
)


def _load_runs(path: Path) -> tuple[ReviewRunRecord, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        ReviewRunRecord(
            case_id=str(item["case_id"]),
            arm=str(item["arm"]),
            findings=tuple(item.get("findings", ())),
            known_defects_found=tuple(item.get("known_defects_found", ())),
            known_defects_total=int(item.get("known_defects_total", 0)),
            cost_usd=float(item.get("cost_usd", 0.0)),
            critic_cost_usd=float(item.get("critic_cost_usd", 0.0)),
            latency_ms=float(item.get("latency_ms", 0.0)),
            model_id=str(item.get("model_id", "")),
            temperature=float(item.get("temperature", 0.0)),
            token_budget=int(item.get("token_budget", 0)),
            workspace_budget_seconds=int(item.get("workspace_budget_seconds", 0)),
            base_sha=str(item.get("base_sha", "")),
            head_sha=str(item.get("head_sha", "")),
            improvement_code=str(item.get("improvement_code", "")),
            changed_paths=tuple(str(path) for path in item.get("changed_paths", ())),
        )
        for item in payload.get("runs", ())
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args()

    controlled = load_controlled_cases()
    expected_case_shas = load_expected_case_shas()
    required_case_ids = {
        str(item["pr_id"]) for item in load_pr_corpus()
    } | {item.case_id for item in controlled}
    report = evaluate_runs(
        _load_runs(args.runs),
        controlled_case_ids={item.case_id for item in controlled},
        required_case_ids=required_case_ids,
        expected_case_shas=expected_case_shas,
    )
    rendered = render_markdown(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
