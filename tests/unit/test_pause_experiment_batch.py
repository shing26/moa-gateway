from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from bench.experiment import (
    BatchRunner,
    BatchStore,
    ExperimentStore,
    RunMetrics,
    TrialEvidence,
    load_protocol,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "pause_experiment_batch.py"


class CancelOnceExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, spec, trial_index: int):
        self.calls += 1
        if self.calls == 2:
            raise asyncio.CancelledError
        return TrialEvidence(
            status="pass",
            metrics=RunMetrics(llm_calls=1, prompt_tokens=10),
        )


def test_pause_orphan_clears_running_cells_and_restores_progress(
    tmp_path: Path,
) -> None:
    protocol = replace(
        load_protocol(),
        tasks=load_protocol().tasks[:1],
        repeats=replace(load_protocol().repeats, formal=2),
    )
    root = tmp_path / "experiments"
    experiments = ExperimentStore(root)
    batch_store = BatchStore(root)
    try:
        asyncio.run(
            BatchRunner(experiments, protocol).run(
                phase="formal",
                arm_names=("L0",),
                batch_id="orphan-batch",
                executor_factory=lambda spec: CancelOnceExecutor(),
            )
        )
    except asyncio.CancelledError:
        pass

    manifest = batch_store.load("orphan-batch")
    stale_cell = replace(
        manifest.cells[0],
        status="running",
        trial_count=0,
        status_counts={},
        error="",
    )
    batch_store.write(
        replace(
            manifest,
            status="running",
            cells=(stale_cell,),
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--batch-id",
            "orphan-batch",
            "--out",
            str(root),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    paused = batch_store.load("orphan-batch")
    assert paused.status == "paused"
    assert paused.cells[0].status == "pending"
    assert paused.cells[0].trial_count == 1
    assert paused.cells[0].status_counts == {"pass": 1}
    assert "runner exited" in paused.cells[0].error
