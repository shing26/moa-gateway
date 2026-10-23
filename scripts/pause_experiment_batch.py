"""Mark an orphaned experiment batch as paused without changing its cells.

Use this only after the process that owned the batch has exited. Active batches
must be cancelled through their runner so in-flight Trial state is recorded.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.experiment import BatchManifest, BatchStore, ExperimentStore


def pause_orphaned_batch(
    batches: BatchStore,
    experiments: ExperimentStore,
    batch_id: str,
) -> BatchManifest:
    manifest = batches.load(batch_id)
    if manifest.status == "completed":
        raise ValueError("已完成批次不能标记为 paused")

    cells = []
    for cell in manifest.cells:
        progress = cell
        if experiments.manifest_path(cell.experiment_id).exists():
            experiment = experiments.load_manifest(cell.experiment_id)
            progress = replace(
                progress,
                trial_count=experiment.trial_count,
                status_counts=dict(experiment.status_counts),
            )
        if progress.status == "running":
            progress = replace(
                progress,
                status="pending",
                error="Batch paused after runner exited",
            )
        cells.append(progress)

    paused = replace(
        manifest,
        status="paused",
        updated_at=datetime.now(UTC).isoformat(),
        cells=tuple(cells),
    )
    batches.write(paused)
    return paused


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="暂停已无进程持有的实验批次")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--out", default=".bench-experiments")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.out)
    store = BatchStore(root)
    manifest = store.load(args.batch_id)
    paused = pause_orphaned_batch(
        store,
        ExperimentStore(root),
        args.batch_id,
    )
    print(f"{manifest.batch_id}: {manifest.status} -> paused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
