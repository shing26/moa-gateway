"""The baseline runner's command/env construction (issue #95 snapshot)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.schema import EvalCase
from scripts.run_eval_baseline import (
    ENV_OVERRIDES,
    GATE_STATE_ARM,
    build_command,
    build_env,
    case_arm,
    prepare_report,
    run_case,
)


def _case(**kw) -> EvalCase:
    base = {
        "id": "biz-x",
        "class_": "business",
        "source": "self",
        "base_sha": "a" * 40,
        "goal": "do the thing",
        "allowed_paths": ("a.py",),
        "oracle_type": "script",
        "verify_commands": ("python -m pytest tests/unit/test_a.py -q",),
    }
    base.update(kw)
    return EvalCase(**base)


def _probe_case(**kw) -> EvalCase:
    base = {
        "id": "exc-postgres-down",
        "class_": "boundary",
        "source": "synthesized; probe",
        "base_sha": "a" * 40,
        "goal": "(no LLM) recovery",
        "allowed_paths": (),
        "oracle_type": "script",
        "verify_commands": (),
        "oracle_checks": (
            "recover_registers_once",
            "second_recover_idempotent",
            "no_duplicate_remote_write",
        ),
        "tags": ("l1-harness", "gate-state-probe"),
    }
    base.update(kw)
    return EvalCase(**base)


def test_command_uses_the_case_baseline_and_goal() -> None:
    case = _case()
    cmd = build_command(case, "20260101-000000")

    assert cmd[:4] == [sys.executable, "-m", "app.cli", "delivery"]
    assert "--base-sha" in cmd and cmd[cmd.index("--base-sha") + 1] == "a" * 40
    assert cmd[cmd.index("--goal") + 1] == "do the thing"
    assert cmd[cmd.index("--branch") + 1] == "eval/biz-x-20260101-000000"
    assert cmd[cmd.index("--base-branch") + 1] == "main"


def test_env_pins_the_frozen_overrides_and_the_case_verify_command() -> None:
    env = build_env(_case(), {"PATH": "x", "CHANGE_AGENT_PUSH_ENABLED": "0"})

    for key, value in ENV_OVERRIDES.items():
        assert env[key] == value, key
    assert env["PATH"] == "x"
    assert env["CHANGE_AGENT_VERIFY_COMMAND"] == "python -m pytest tests/unit/test_a.py -q"


def test_env_clears_verify_command_when_case_has_none() -> None:
    """A case without a verify command must not inherit .env's default.

    Probe cases never spawn, but the env builder must stay fail-closed: an empty
    value makes the CLI refuse instead of gating on an undeclared command.
    """
    env = build_env(_case(verify_commands=()), {"CHANGE_AGENT_VERIFY_COMMAND": "python -m pytest -q"})

    assert env["CHANGE_AGENT_VERIFY_COMMAND"] == ""


def test_case_arm_splits_live_and_gate_state() -> None:
    assert case_arm(_case()) == "live"
    assert case_arm(_probe_case()) == "gate_state"
    # A live gate-state case declares its neutral command and still runs.
    assert (
        case_arm(
            _case(
                class_="security",
                verify_commands=("python -c pass",),
                tags=("l2-live", "gate-state-live"),
            )
        )
        == "live"
    )


def test_gate_state_case_is_recorded_but_never_spawned(tmp_path: Path) -> None:
    """A probe case runs in-process: no child, no log, a real verdict row."""
    row = run_case(
        _probe_case(),
        stamp="20260101-000000",
        timeout_s=0.001,
        log_dir=tmp_path,
    )

    assert row["arm"] == "gate_state"
    assert row["state"] in {"gate_state_passed", "gate_state_failed"}
    assert row["state"] == "gate_state_passed"
    assert row["run_id"] == ""
    assert row["exit_code"] is None
    assert row["gate_state"]["checks"][0]["name"] == "recover_registers_once"
    assert [check["name"] for check in row["gate_state"]["checks"]] == list(
        _probe_case().oracle_checks
    )
    assert row["reason"] == GATE_STATE_ARM
    assert list(tmp_path.iterdir()) == []


def test_probe_verdict_is_exactly_the_oracle_checks() -> None:
    """A probe whose names do not match the corpus is a hard error."""
    case = _probe_case(oracle_checks=("wrong_name",))

    row = run_case(case, stamp="s", timeout_s=0.001, log_dir=Path("."))

    assert row["state"] == "gate_state_failed"
    assert [c["name"] for c in row["gate_state"]["checks"]] == ["wrong_name"]
    assert "probe checks" in row["gate_state"]["checks"][0]["detail"]


def test_prepare_report_refreshes_metadata_and_keeps_rows(tmp_path: Path) -> None:
    """A report from an older protocol must not keep its stale model or arm."""
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "protocol": "old",
                "model": "",
                "deferred_arm": "old reason",
                "rows": [{"case_id": "biz-x"}],
            }
        ),
        encoding="utf-8",
    )

    report = prepare_report(path, "20260101-000000")

    assert report["protocol"] == "issue #95 (v1.5 gate-state)"
    assert report["model"] == ENV_OVERRIDES["CHANGE_AGENT_MODEL"]
    assert report["stamp"] == "20260101-000000"
    assert report["rows"] == [{"case_id": "biz-x"}]
    assert report["gate_state_arm"] == GATE_STATE_ARM
    assert "deferred_arm" not in report


def test_prepare_report_survives_a_report_without_rows(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"protocol": "old"}), encoding="utf-8")

    assert prepare_report(path, "s")["rows"] == []


def test_runner_never_lets_the_child_see_a_tty() -> None:
    """The push gate's denial depends on a non-TTY stdin; assert it in source."""
    source = (ROOT / "scripts" / "run_eval_baseline.py").read_text(encoding="utf-8")
    assert "stdin=subprocess.DEVNULL" in source
