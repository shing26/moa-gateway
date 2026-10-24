from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from bench.experiment import (
    BatchRunner,
    BatchStore,
    ExperimentStore,
    RunMetrics,
    TrialEvidence,
    load_protocol,
)
from bench.experiment.cross_model import build_cross_model_report
from bench.run_batch import main as batch_cli

PROTOCOL_V4 = Path("bench/protocols/multi-agent-first-batch-v4.json")


class CrossModelExecutor:
    async def execute(self, spec, trial_index: int):
        arm = str(spec.config["arm"])
        model = str(spec.model["model"])
        passing_arm = "L3" if model == "model-a" else "L0"
        return TrialEvidence(
            status="pass" if arm == passing_arm else "fail",
            metrics=RunMetrics(
                llm_calls=1,
                prompt_tokens=10,
                completion_tokens=5,
            ),
        )


class ZeroDifferenceExecutor:
    async def execute(self, spec, trial_index: int):
        return TrialEvidence(
            status="fail",
            metrics=RunMetrics(
                llm_calls=1,
                prompt_tokens=10,
                completion_tokens=5,
            ),
        )


def _protocol(model: str, *, task_count: int = 1):
    base = load_protocol(PROTOCOL_V4)
    assert base.statistics is not None
    return replace(
        base,
        version=f"{base.version}-{model}",
        tasks=base.tasks[:task_count],
        repeats=replace(base.repeats, baseline=1),
        model=replace(base.model, model=model),
        statistics=replace(
            base.statistics,
            planned_task_count=task_count,
            planned_repeats_per_cell=1,
            min_valid_repeats_per_cell=1,
            bootstrap_resamples=200,
        ),
    )


def _same_name_protocol_configs(protocol, *, temperature: float, max_tokens: int):
    return replace(
        protocol,
        version=f"{protocol.version}-temp{temperature}-tokens{max_tokens}",
        model=replace(
            protocol.model,
            temperature=temperature,
            max_tokens=max_tokens,
        ),
    )


def _build_model_batches(
    root: Path,
    *,
    task_count: int = 1,
) -> tuple[str, str]:
    for model, batch_id in (
        ("model-a", "model-a-batch"),
        ("model-b", "model-b-batch"),
    ):
        asyncio.run(
            BatchRunner(
                ExperimentStore(root),
                _protocol(model, task_count=task_count),
            ).run(
                phase="baseline",
                arm_names=("L0", "L3"),
                batch_id=batch_id,
                executor_factory=lambda spec: CrossModelExecutor(),
            )
        )
    return "model-a-batch", "model-b-batch"


def test_cross_model_report_keeps_inference_inside_each_model(
    tmp_path,
) -> None:
    root = tmp_path / "experiments"
    batch_ids = _build_model_batches(root, task_count=8)

    report = build_cross_model_report(
        batch_ids,
        output_root=root,
        protocol=_protocol("model-a", task_count=8),
    )

    assert report.models == ("model-a", "model-b")
    assert len(report.matrix) == 4
    assert {item.model for item in report.paired} == {"model-a", "model-b"}
    assert report.to_dict()["cross_model_inferential_claim"] is False
    assert report.to_dict()["analysis_authorized_by_model"] == {
        "model-a": True,
        "model-b": True,
    }
    assert report.to_dict()["statistical_claims_by_model"] == {
        "model-a": True,
        "model-b": True,
    }
    matrix = {(item.model, item.arm): item for item in report.matrix}
    assert matrix[("model-a", "L3")].pass_rate == 1.0
    assert matrix[("model-a", "L0")].pass_rate == 0.0
    assert matrix[("model-b", "L3")].pass_rate == 0.0
    assert matrix[("model-b", "L0")].pass_rate == 1.0


def test_cross_model_zero_difference_does_not_authorize_significance(
    tmp_path,
) -> None:
    root = tmp_path / "experiments"
    for model, batch_id in (
        ("model-a", "model-a-batch"),
        ("model-b", "model-b-batch"),
    ):
        asyncio.run(
            BatchRunner(ExperimentStore(root), _protocol(model)).run(
                phase="baseline",
                arm_names=("L0", "L3"),
                batch_id=batch_id,
                executor_factory=lambda spec: ZeroDifferenceExecutor(),
            )
        )

    report = build_cross_model_report(
        ("model-a-batch", "model-b-batch"),
        output_root=root,
        protocol=_protocol("model-a"),
    )

    assert report.to_dict()["analysis_authorized_by_model"] == {
        "model-a": True,
        "model-b": True,
    }
    assert report.to_dict()["statistical_claims_by_model"] == {
        "model-a": False,
        "model-b": False,
    }


def test_cross_model_distinguishes_same_model_protocol_configs(
    tmp_path,
) -> None:
    root = tmp_path / "experiments"
    base = _protocol("model-a")
    configs = (
        _same_name_protocol_configs(base, temperature=1.0, max_tokens=4096),
        _same_name_protocol_configs(base, temperature=0.0, max_tokens=512),
    )
    for index, protocol in enumerate(configs):
        asyncio.run(
            BatchRunner(
                ExperimentStore(root),
                protocol,
            ).run(
                phase="baseline",
                arm_names=("L0", "L3"),
                batch_id=f"same-model-config-{index}",
                executor_factory=lambda spec: CrossModelExecutor(),
            )
        )

    report = build_cross_model_report(
        ("same-model-config-0", "same-model-config-1"),
        output_root=root,
        protocol=configs[0],
    )

    assert len(report.models) == 2
    assert set(report.models) == {"model-a"}
    assert len(report.paired) == 2


def test_cross_model_cli_writes_json_and_markdown(tmp_path, capsys) -> None:
    root = tmp_path / "experiments"
    batch_ids = _build_model_batches(root)

    code = batch_cli(
        [
            "compare",
            "--protocol",
            str(PROTOCOL_V4),
            "--batch-id",
            batch_ids[0],
            "--batch-id",
            batch_ids[1],
            "--out",
            str(root),
        ]
    )

    assert code == 0
    output = capsys.readouterr().out
    assert '"cross_model_inferential_claim": false' in output
    assert (root / "_comparisons").is_dir()
    comparison_dir = next((root / "_comparisons").iterdir())
    assert (comparison_dir / "comparison.json").is_file()
    assert (comparison_dir / "comparison.md").is_file()


def test_cross_model_report_rejects_incomplete_batch(tmp_path) -> None:
    root = tmp_path / "experiments"
    batch_ids = _build_model_batches(root)
    store = BatchStore(root)
    manifest = store.load(batch_ids[1])
    store.write(replace(manifest, status="paused"))

    with pytest.raises(ValueError, match="不能比较"):
        build_cross_model_report(
            batch_ids,
            output_root=root,
            protocol=_protocol("model-a"),
        )
