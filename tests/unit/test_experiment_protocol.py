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
