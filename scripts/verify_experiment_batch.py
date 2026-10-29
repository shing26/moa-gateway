"""Verify a completed experiment batch against its immutable Run Records."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.experiment_results import ExperimentResultService
from bench.experiment import (
    BatchStore,
    ExperimentStore,
    build_batch_report,
    load_protocol,
)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{path} 不是 JSON 对象")
    return payload


def _diff(
    expected: Any,
    actual: Any,
    *,
    path: str = "$",
) -> list[str]:
    if type(expected) is not type(actual):
        return [f"{path}: 类型 {type(actual).__name__} != {type(expected).__name__}"]
    if isinstance(expected, dict):
        differences: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}"
            if key not in expected:
                differences.append(f"{child}: 多出字段")
            elif key not in actual:
                differences.append(f"{child}: 缺少字段")
            else:
                differences.extend(_diff(expected[key], actual[key], path=child))
        return differences
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: 长度 {len(actual)} != {len(expected)}"]
        differences = []
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            differences.extend(_diff(left, right, path=f"{path}[{index}]"))
        return differences
    if expected != actual:
        return [f"{path}: {actual!r} != {expected!r}"]
    return []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_experiment_batch(
    *,
    output_root: Path | str,
    batch_id: str,
    protocol_path: Path | str,
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    batches = BatchStore(root)
    experiments = ExperimentStore(root)
    manifest = batches.load(batch_id)
    if manifest.status != "completed":
        raise ValueError(f"批次状态为 {manifest.status!r}，只有 completed 可验收")

    protocol = load_protocol(protocol_path)
    if protocol.config_hash != manifest.protocol_hash:
        raise ValueError(
            "协议哈希不一致: "
            f"{protocol.config_hash} != {manifest.protocol_hash}"
        )

    report_path = batches.report_json_path(batch_id)
    if not report_path.exists():
        raise FileNotFoundError(f"批次报告不存在: {report_path}")
    saved = _load_json(report_path)
    rebuilt = build_batch_report(manifest, experiments, protocol).to_dict()
    differences = _diff(rebuilt, saved)
    if differences:
        preview = "; ".join(differences[:8])
        raise ValueError(f"report.json 与 Run Record 重算不一致: {preview}")

    results = ExperimentResultService(root)
    record_count = 0
    for cell in manifest.cells:
        if cell.status != "completed":
            raise ValueError(f"{cell.cell_id} 未完成")
        experiment = experiments.load_manifest(cell.experiment_id)
        records = experiments.load_experiment_records(cell.experiment_id)
        if len(records) != cell.expected_trials:
            raise ValueError(
                f"{cell.cell_id} Trial 数 {len(records)} != {cell.expected_trials}"
            )
        if tuple(record.trial_index for record in records) != tuple(
            range(cell.expected_trials)
        ):
            raise ValueError(f"{cell.cell_id} Trial 序号不连续")
        if experiment.config_hash != records[0].config_hash:
            raise ValueError(f"{cell.cell_id} Experiment 与 Run Record 哈希不一致")
        for record in records:
            api_record = results.run(
                batch_id,
                cell.experiment_id,
                record.trial_index,
            )
            relative_path = api_record.pop("_relative_path", "")
            if api_record != record.to_dict():
                raise ValueError(
                    f"{cell.cell_id} Trial {record.trial_index} 只读 API "
                    "与 Run Record 不一致"
                )
            expected_relative = (
                Path("..")
                / cell.experiment_id
                / "trials"
                / f"trial-{record.trial_index:03d}.json"
            )
            if Path(relative_path).parts[-3:] != expected_relative.parts[-3:]:
                raise ValueError(
                    f"{cell.cell_id} Trial {record.trial_index} API 路径异常: "
                    f"{relative_path}"
                )
        record_count += len(records)

    sources = (saved.get("report") or {}).get("sources") or []
    if not isinstance(sources, list) or not all(
        isinstance(item, str) for item in sources
    ):
        raise ValueError("report.sources 必须是字符串数组")
    for source in sources:
        path = Path(source)
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"报告来源越出实验根目录: {source}")
        if not resolved.is_file():
            raise FileNotFoundError(f"报告来源不存在: {source}")

    ui_payload = results.batch(batch_id)
    api_differences = _diff(saved, ui_payload.get("report"))
    if api_differences:
        preview = "; ".join(api_differences[:8])
        raise ValueError(f"只读 API 报告与 report.json 不一致: {preview}")
    if ui_payload.get("manifest") != manifest.to_dict():
        raise ValueError("只读 API Manifest 与 batch.json 不一致")

    return {
        "ok": True,
        "batch_id": batch_id,
        "protocol_hash": manifest.protocol_hash,
        "report_sha256": _sha256(report_path),
        "expected_runs": sum(cell.expected_trials for cell in manifest.cells),
        "record_count": record_count,
        "completed_cells": len(manifest.cells),
        "analysis_authorized": bool(saved.get("analysis_authorized")),
        "statistical_claims": bool(saved.get("statistical_claims")),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对账完整实验批次的报告、Run Record 与只读 API"
    )
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--out", default=".bench-experiments")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = verify_experiment_batch(
            output_root=args.out,
            batch_id=args.batch_id,
            protocol_path=args.protocol,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
