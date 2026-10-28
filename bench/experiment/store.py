"""本地不可变实验证据仓。"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
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
            expected_trial_ids = tuple(
                f"{manifest.experiment_id}-trial-{index:03d}"
                for index in range(existing.trial_count, manifest.trial_count)
            )
            expected_trial_paths = tuple(
                f"trials/trial-{index:03d}.json"
                for index in range(existing.trial_count, manifest.trial_count)
            )
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
                or manifest.trial_ids[existing.trial_count :]
                != expected_trial_ids
                or manifest.trial_paths[existing.trial_count :]
                != expected_trial_paths
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
            self._validate_new_trial_evidence(existing, manifest)
        self._atomic_write(path, manifest.to_dict(), overwrite=True)
        return path

    def _validate_new_trial_evidence(
        self,
        existing: ExperimentManifest,
        manifest: ExperimentManifest,
    ) -> None:
        """Reject manifest progress that has no matching immutable record."""
        experiment_dir = self.experiment_dir(manifest.experiment_id).resolve()
        added_counts: Counter[str] = Counter()
        for index in range(existing.trial_count, manifest.trial_count):
            expected_id = f"{manifest.experiment_id}-trial-{index:03d}"
            expected_path = f"trials/trial-{index:03d}.json"
            if (
                manifest.trial_ids[index] != expected_id
                or manifest.trial_paths[index] != expected_path
            ):
                raise FileExistsError(
                    f"Experiment manifest 的 Trial 身份与约定不符: {expected_path}"
                )
            trial_path = (experiment_dir / expected_path).resolve()
            if not trial_path.is_relative_to(experiment_dir) or not trial_path.is_file():
                raise FileExistsError(
                    f"Experiment manifest 引用的 Run Record 不存在: {expected_path}"
                )
            record = self.load_record(trial_path)
            if (
                record.experiment_id != manifest.experiment_id
                or record.trial_id != expected_id
                or record.trial_index != index
                or record.config_hash != manifest.config_hash
            ):
                raise FileExistsError(
                    f"Run Record 与 manifest 证据不符: {expected_path}"
                )
            added_counts[record.status] += 1
        expected_counts = Counter(existing.status_counts) + added_counts
        if dict(sorted(expected_counts.items())) != dict(
            sorted(manifest.status_counts.items())
        ):
            raise FileExistsError(
                "Experiment manifest 的 status_counts 与 Run Record 不符"
            )

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
