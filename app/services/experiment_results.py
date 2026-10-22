"""Read-only access to persisted multi-agent experiment results."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DEFAULT_ROOT = Path(".bench-experiments")


def _safe_id(value: str, label: str) -> str:
    text = value.strip()
    if not _SAFE_ID.fullmatch(text):
        raise ValueError(f"非法 {label}: {value!r}")
    return text


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{path} 不是 JSON 对象")
    return payload


class ExperimentResultService:
    """Discover immutable batch and Run Record artifacts without mutating them."""

    def __init__(self, root: Path | str | None = None) -> None:
        configured = os.getenv("BENCH_EXPERIMENTS_DIR", "").strip()
        self.root = Path(root or configured or _DEFAULT_ROOT).resolve()

    def list_batches(self) -> list[dict[str, Any]]:
        batches_root = self.root / "_batches"
        if not batches_root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for manifest_path in batches_root.glob("*/batch.json"):
            try:
                manifest = _load_json(manifest_path)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            batch_id = str(manifest.get("batch_id", "") or manifest_path.parent.name)
            report_path = manifest_path.parent / "report.json"
            report: dict[str, Any] = {}
            if report_path.exists():
                try:
                    report = _load_json(report_path)
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    report = {}
            cells = list(manifest.get("cells") or [])
            rows.append(
                {
                    "batch_id": batch_id,
                    "protocol_id": str(manifest.get("protocol_id", "")),
                    "protocol_version": str(manifest.get("protocol_version", "")),
                    "phase": str(manifest.get("phase", "")),
                    "budget_tier": str(manifest.get("budget_tier", "B1") or "B1"),
                    "source_batch_id": str(
                        manifest.get("source_batch_id", "") or ""
                    ),
                    "status": str(manifest.get("status", "")),
                    "updated_at": str(manifest.get("updated_at", "")),
                    "cell_count": len(cells),
                    "completed_cells": sum(
                        1 for item in cells if item.get("status") == "completed"
                    ),
                    "completed_runs": int(report.get("completed_runs", 0) or 0),
                    "expected_runs": int(report.get("expected_runs", 0) or 0),
                    "missing_runs": int(report.get("missing_runs", 0) or 0),
                }
            )
        return sorted(
            rows,
            key=lambda item: (item["updated_at"], item["batch_id"]),
            reverse=True,
        )

    def batch(self, batch_id: str) -> dict[str, Any]:
        safe_batch = _safe_id(batch_id, "batch_id")
        batch_dir = self.root / "_batches" / safe_batch
        manifest_path = batch_dir / "batch.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"批次不存在: {safe_batch}")
        manifest = _load_json(manifest_path)
        report_path = batch_dir / "report.json"
        report = _load_json(report_path) if report_path.exists() else {}
        cells: list[dict[str, Any]] = []
        for raw in list(manifest.get("cells") or []):
            if not isinstance(raw, dict):
                continue
            cell = dict(raw)
            runs: list[dict[str, Any]] = []
            experiment_id = str(cell.get("experiment_id", "") or "")
            if _SAFE_ID.fullmatch(experiment_id):
                trial_dir = self.root / experiment_id / "trials"
                if trial_dir.is_dir():
                    for path in sorted(trial_dir.glob("trial-*.json")):
                        try:
                            record = _load_json(path)
                        except (
                            OSError,
                            TypeError,
                            ValueError,
                            json.JSONDecodeError,
                        ):
                            continue
                        runs.append(
                            {
                                "trial_index": int(
                                    record.get("trial_index", 0) or 0
                                ),
                                "status": str(record.get("status", "")),
                                "duration_ms": float(
                                    record.get("duration_ms", 0.0) or 0.0
                                ),
                                "metrics": dict(record.get("metrics") or {}),
                                "path": path.relative_to(self.root).as_posix(),
                            }
                        )
            cell["runs"] = runs
            cells.append(cell)
        return {
            "manifest": manifest,
            "report": report,
            "cells": cells,
        }

    def run(
        self, batch_id: str, experiment_id: str, trial_index: int
    ) -> dict[str, Any]:
        safe_batch = _safe_id(batch_id, "batch_id")
        safe_experiment = _safe_id(experiment_id, "experiment_id")
        if trial_index < 0:
            raise ValueError("trial_index 不能为负数")
        batch = self.batch(safe_batch)
        allowed = {
            str(cell.get("experiment_id", ""))
            for cell in batch["cells"]
            if isinstance(cell, dict)
        }
        if safe_experiment not in allowed:
            raise FileNotFoundError(
                f"Experiment 不属于批次 {safe_batch}: {safe_experiment}"
            )
        path = (
            self.root
            / safe_experiment
            / "trials"
            / f"trial-{trial_index:03d}.json"
        )
        if not path.exists():
            raise FileNotFoundError(
                f"Run Record 不存在: {safe_experiment}/trial-{trial_index:03d}"
            )
        record = _load_json(path)
        record["_relative_path"] = path.relative_to(self.root).as_posix()
        return record


__all__ = ["ExperimentResultService"]
