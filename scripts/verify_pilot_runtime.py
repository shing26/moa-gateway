"""Check whether a completed pilot supports launching the formal batch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from bench.experiment import load_pilot_runtime_gate, load_protocol


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查 completed pilot 的完成度、证据和正式批次耗时投影"
    )
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--out", default=".bench-experiments")
    parser.add_argument("--max-pilot-hours", type=float, default=4.0)
    parser.add_argument("--max-projected-formal-hours", type=float, default=72.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        protocol = load_protocol(args.protocol)
        result = load_pilot_runtime_gate(
            args.out,
            batch_id=args.batch_id,
            protocol=protocol,
            max_pilot_hours=args.max_pilot_hours,
            max_projected_formal_hours=args.max_projected_formal_hours,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
