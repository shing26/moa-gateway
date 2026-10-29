"""Run R0/R1/R1+I cases and append raw records for human review."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.review_value_runner import execute_matrix, write_run_records


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--arm",
        action="append",
        choices=("R0", "R1", "R1+I"),
        required=True,
        help="repeat to run multiple arms; use a separate R1+I invocation after selecting the improvement",
    )
    parser.add_argument("--case-id", action="append", default=None)
    parser.add_argument("--improvement-code", default="")
    parser.add_argument("--changed-path", default="")
    parser.add_argument("--improvement-base-ref", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    records = await execute_matrix(
        arms=tuple(dict.fromkeys(args.arm)),
        case_ids=args.case_id,
        improvement_code=args.improvement_code,
        changed_path=args.changed_path,
        improvement_base_ref=args.improvement_base_ref,
    )
    write_run_records(args.output, records, replace_existing=args.replace)
    print(f"wrote {len(records)} run records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
