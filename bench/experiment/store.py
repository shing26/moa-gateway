"""本地不可变实验证据仓。"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from bench.experiment.types import (
    ExperimentManifest,
    ExperimentSpec,
    RunRecord,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ExperimentStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def experiment_dir(self, experiment_id: str) -> Path:
        if not _SAFE_ID.fullmatch(experiment_id):
            raise ValueError(f"非法 experiment_id: {experiment_id!r}")
        return self.root / experiment_id

    def manifest_path(self, experiment_id: str) -> Path:
        return self.experiment_dir(experiment_id) / "experiment.json"

    def trial_path(self, experiment_id: str, trial_index: int) -> Path:
        return (
            self.experiment_dir(experiment_id)
            / "trials"
            / f"trial-{int(trial_index):03d}.json"
        )

    def write_manifest(self, manifest: ExperimentManifest) -> Path:
        path = self.manifest_path(manifest.experiment_id)
        if path.exists():
            existing = self.load_manifest(manifest.experiment_id)
            if (
                manifest.config_hash != existing.config_hash
                or manifest.created_at != existing.created_at
                or manifest.spec != existing.spec
                or manifest.trial_count < existing.trial_count
                or manifest.trial_count > existing.spec.trials
                or manifest.trial_ids[: existing.trial_count]
                != existing.trial_ids
                or manifest.trial_paths[: existing.trial_count]
                != existing.trial_paths
                or len(manifest.trial_ids) != manifest.trial_count
                or len(manifest.trial_paths) != manifest.trial_count
                or sum(manifest.status_counts.values()) != manifest.trial_count
                or any(
                    manifest.status_counts.get(status, 0) < count
                    for status, count in existing.status_counts.items()
                )
            ):
                raise FileExistsError(
                    f"Experiment manifest 已存在，不得改写配置、身份或回退进度: {path}"
                )
        self._atomic_write(path, manifest.to_dict(), overwrite=True)
        return path

    def write_record(self, record: RunRecord) -> Path:
        path = self.trial_path(record.experiment_id, record.trial_index)
        self._atomic_write(path, record.to_dict(), overwrite=False)
        return path

    def load_manifest(self, experiment_id: str) -> ExperimentManifest:
        path = self.manifest_path(experiment_id)
        return ExperimentManifest.from_dict(
            json.loads(path.read_text(encoding="utf-8")), path=path
        )

    def load_record(self, path: Path | str) -> RunRecord:
        target = Path(path)
        return RunRecord.from_dict(
            json.loads(target.read_text(encoding="utf-8")), path=target
        )

    def load_experiment_records(self, experiment_id: str) -> tuple[RunRecord, ...]:
        manifest = self.load_manifest(experiment_id)
        base = self.experiment_dir(experiment_id)
        return tuple(
            self.load_record(base / relative) for relative in manifest.trial_paths
        )

    @staticmethod
    def _atomic_write(path: Path, payload: dict, *, overwrite: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise FileExistsError(f"Run Record 已存在，拒绝覆盖: {path}")
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()


__all__ = ["ExperimentStore"]
