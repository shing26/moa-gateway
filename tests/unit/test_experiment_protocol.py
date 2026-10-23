from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from bench.arms import arm_names
from bench.experiment.protocol import (
    DEFAULT_PROTOCOL_PATH,
    PROTOCOL_SCHEMA_VERSION,
    load_protocol,
    protocol_from_dict,
)


EXPECTED_TASKS = (
    ("0896629", "E1"),
    ("60c64ed", "E1"),
    ("988fccb", "E1"),
    ("a746b81", "E1"),
    ("bd4f186", "E1"),
    ("e3-c1-budget-ttl-refresh", "E3"),
    ("e3-c2-keyword-normalization", "E3"),
    ("e3-c3-binary-write-review", "E3"),
)
STATS_PROTOCOL_PATH = (
    DEFAULT_PROTOCOL_PATH.parent / "multi-agent-first-batch-v4.json"
)
LOCAL_PROTOCOL_PATH = (
    DEFAULT_PROTOCOL_PATH.parent / "multi-agent-first-batch-v5-local.json"
)


def test_default_protocol_freezes_the_first_batch() -> None:
    protocol = load_protocol()

    assert protocol.schema_version == PROTOCOL_SCHEMA_VERSION
    assert protocol.protocol_id == "multi-agent-first-batch"
    assert protocol.version == "3"
    assert tuple((task.task_id, task.origin) for task in protocol.tasks) == EXPECTED_TASKS
    assert tuple(arm.name for arm in protocol.arms) == arm_names()
    assert protocol.model.provider == "nvidia"
    assert protocol.model.model == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert protocol.model.max_tokens >= 4096
    assert protocol.budget.axis == "tokens"
    assert protocol.budget.b1.max_steps >= 40
    assert protocol.budget.b1.max_tokens >= 400_000
    assert protocol.budget.b3.max_steps > protocol.budget.b1.max_steps
    assert protocol.budget.b3.max_tokens > protocol.budget.b1.max_tokens
    assert protocol.repeats.formal == 3
    assert protocol.retry.max_error_retries == 1
    assert protocol.reporting.statistical_claims is False


def test_default_protocol_matches_task_files_and_the_arm_registry() -> None:
    protocol = load_protocol()
    tasks_dir = DEFAULT_PROTOCOL_PATH.parents[1] / "tasks"

    for declared in protocol.tasks:
        payload = json.loads(
            (tasks_dir / f"{declared.task_id}.task.json").read_text(encoding="utf-8")
        )
        assert payload["task_id"] == declared.task_id
        assert payload["source"] == declared.origin


def test_protocol_hash_is_stable_and_covers_semantic_changes() -> None:
    payload = json.loads(DEFAULT_PROTOCOL_PATH.read_text(encoding="utf-8"))
    baseline = protocol_from_dict(payload)
    reordered = protocol_from_dict(
        json.loads(json.dumps(payload, sort_keys=True), object_pairs_hook=dict)
    )

    assert reordered.config_hash == baseline.config_hash

    changed_payload = copy.deepcopy(payload)
    changed_payload["model"]["temperature"] = 0.2
    changed_model = protocol_from_dict(changed_payload)
    assert changed_model.config_hash != baseline.config_hash

    changed_payload = copy.deepcopy(payload)
    changed_payload["budget"]["b1"]["max_tokens"] += 1
    changed_budget = protocol_from_dict(changed_payload)
    assert changed_budget.config_hash != baseline.config_hash

    changed_payload = copy.deepcopy(payload)
    changed_payload["tasks"].reverse()
    changed_task_order = protocol_from_dict(changed_payload)
    assert changed_task_order.config_hash != baseline.config_hash


def test_protocol_builds_cell_specs_with_hash_bound_identity() -> None:
    protocol = load_protocol()
    spec = protocol.experiment_spec("0896629", "L3", batch_id="batch-unit")
    changed = replace(protocol, version="999").experiment_spec(
        "0896629", "L3", batch_id="batch-unit"
    )

    assert spec.adapter == "bench:L3"
    assert spec.trials == protocol.repeats.baseline
    assert spec.config["protocol_id"] == protocol.protocol_id
    assert spec.config["protocol_hash"] == protocol.config_hash
    assert spec.config["task_id"] == "0896629"
    assert spec.config["arm"] == "L3"
    assert spec.experiment_id.startswith("exp-")
    assert spec.experiment_id != changed.experiment_id
    assert spec.config_hash != changed.config_hash


def test_b3_spec_has_distinct_identity_budget_and_baseline_transport() -> None:
    protocol = load_protocol()
    b1 = protocol.experiment_spec("0896629", "L3", batch_id="batch-unit")
    b3 = protocol.experiment_spec(
        "0896629",
        "L3",
        phase="formal",
        batch_id="batch-unit",
        budget_tier="B3",
        source_batch_id="source-batch",
        baseline_cost_usd=1.25,
        baseline_tokens=1234,
    )

    # B1 的身份不能因新增 B3 能力而漂移；正在恢复的批次依赖这个稳定值。
    assert b1.experiment_id == "exp-7b67546e621d44ac"
    assert b1.config_hash == (
        "8f886cd47f3113013c9bee2c41cdddb523c31902b474b510c4c74068c9a5b2b8"
    )
    assert b1.config["budget_tier"] == "B1"
    assert b3.experiment_id != b1.experiment_id
    assert b3.config_hash != b1.config_hash
    assert b3.config["budget_tier"] == "B3"
    assert b3.config["budget"] == protocol.budget.b3.to_dict()
    assert b3.config["b3_source_batch_id"] == "source-batch"
    assert b3.config["baseline_cost_usd"] == 1.25
    assert b3.config["baseline_tokens"] == 1234


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload.pop("tasks"),
            "tasks",
        ),
        (
            lambda payload: payload["arms"][0].update(name="L9"),
            "arm",
        ),
        (
            lambda payload: payload["budget"].update(axis="wall-clock"),
            "axis",
        ),
        (
            lambda payload: payload["retry"].update(max_error_retries=-1),
            "max_error_retries",
        ),
    ],
)
def test_protocol_rejects_incomplete_or_unknown_contract_fields(
    mutate, message: str
) -> None:
    payload = json.loads(DEFAULT_PROTOCOL_PATH.read_text(encoding="utf-8"))
    mutate(payload)

    with pytest.raises((TypeError, ValueError), match=message):
        protocol_from_dict(payload)


def test_default_protocol_path_is_stable() -> None:
    assert DEFAULT_PROTOCOL_PATH.name == "multi-agent-first-batch-v3.json"
    assert DEFAULT_PROTOCOL_PATH.exists()


def test_statistics_protocol_has_a_distinct_preregistered_identity() -> None:
    baseline = load_protocol()
    confirmatory = load_protocol(STATS_PROTOCOL_PATH)

    assert confirmatory.version == "4"
    assert confirmatory.config_hash != baseline.config_hash
    assert confirmatory.statistics is not None
    assert confirmatory.statistics.unit == "task"
    assert confirmatory.statistics.primary_metric == "pass_rate"
    assert confirmatory.statistics.candidate_arm == "L3"
    assert confirmatory.statistics.baseline_arm == "L0"
    assert confirmatory.statistics.planned_task_count == 8
    assert confirmatory.statistics.planned_repeats_per_cell == 3
    assert confirmatory.statistics.min_valid_repeats_per_cell == 3
    assert confirmatory.statistics.bootstrap_resamples == 10_000
    assert confirmatory.statistics.sensitivity == (
        "leave_one_task_out",
        "origin_stratified",
    )


def test_statistics_protocol_rejects_impossible_pre_registration() -> None:
    payload = json.loads(STATS_PROTOCOL_PATH.read_text(encoding="utf-8"))
    payload["statistics"]["min_valid_repeats_per_cell"] = 4

    with pytest.raises(ValueError, match="min_valid_repeats_per_cell"):
        protocol_from_dict(payload)


def test_local_second_model_protocol_has_an_independent_identity() -> None:
    confirmatory = load_protocol(STATS_PROTOCOL_PATH)
    local = load_protocol(LOCAL_PROTOCOL_PATH)

    assert local.version == "5-local"
    assert local.config_hash != confirmatory.config_hash
    assert tuple((task.task_id, task.origin) for task in local.tasks) == EXPECTED_TASKS
    assert tuple(arm.name for arm in local.arms) == arm_names()
    assert local.model.provider == "local"
    assert local.model.model == "qwen2.5:3b"
    assert local.model.base_url == "http://127.0.0.1:11434/v1"
    assert local.model.timeout == 300.0
    assert local.model.max_tokens == 4096
    assert local.model.cost_signal is False
    assert local.statistics == confirmatory.statistics
