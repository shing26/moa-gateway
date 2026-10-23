"""Decide whether live pilots satisfy the capacity recovery gate."""

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

from bench.experiment import load_capacity_gate, load_protocol


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查连续 live pilot 是否满足容量恢复门槛"
    )
    parser.add_argument("--protocol", required=True)
    parser.add_argument(
        "--batch-id",
        action="append",
        required=True,
        help="可重复传入；脚本按 Batch Manifest updated_at 排序",
    )
    parser.add_argument("--out", default=".bench-experiments")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        protocol = load_protocol(args.protocol)
        result = load_capacity_gate(
            args.out,
            batch_ids=args.batch_id,
            protocol_hash=protocol.config_hash,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
