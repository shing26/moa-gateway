"""Mark an orphaned experiment batch as paused without changing its cells.

Use this only after the process that owned the batch has exited. Active batches
must be cancelled through their runner so in-flight Trial state is recorded.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.experiment import BatchStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="暂停已无进程持有的实验批次")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--out", default=".bench-experiments")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = BatchStore(Path(args.out))
    manifest = store.load(args.batch_id)
    if manifest.status == "completed":
        raise ValueError("已完成批次不能标记为 paused")
    store.write(replace(manifest, status="paused"))
    print(f"{manifest.batch_id}: {manifest.status} -> paused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
