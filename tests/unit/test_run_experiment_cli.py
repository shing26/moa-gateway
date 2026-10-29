from __future__ import annotations

import json

from bench import run_experiment as cli
from bench.experiment import ExperimentStore


def test_mock_collab_cli_writes_self_contained_run_record(
    tmp_path, capsys
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    out = tmp_path / "records"
    verify = json.dumps(
        ["python", "-c", "print('1 passed')"],
        ensure_ascii=False,
    )

    code = cli.main(
        [
            "collab",
            "--task",
            "读取 sample.py 并报告内容",
            "--trials",
            "1",
            "--mode",
            "mock",
            "--workspace",
            str(workspace),
            "--verify-command-json",
            verify,
            "--out",
            str(out),
            "--experiment-id",
            "exp-cli-unit",
        ]
    )

    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status_counts"] == {"ok": 1}
    record = ExperimentStore(out).load_record(summary["records"][0]["path"])
    assert record.verify_command == ("python", "-c", "print('1 passed')")
    assert record.model == {}
    assert record.config["mode"] == "mock"
    assert record.config["prompt_version"] == "collaboration-prompts-v1"
    assert record.workspace.snapshot_id


def test_cli_compare_reports_controlled_rerun_without_natural_text_diff(
    tmp_path, capsys
) -> None:
    out = tmp_path / "records"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = cli.main(
        [
            "collab",
            "--task",
            "总结一个简单事实",
            "--mode",
            "mock",
            "--workspace",
            str(workspace),
            "--out",
            str(out),
            "--experiment-id",
            "exp-baseline",
        ]
    )
    assert first == 0
    first_payload = json.loads(capsys.readouterr().out)

    second = cli.main(
        [
            "collab",
            "--task",
            "总结一个简单事实",
            "--mode",
            "mock",
            "--workspace",
            str(workspace),
            "--out",
            str(out),
            "--experiment-id",
            "exp-candidate",
            "--compare-to",
            first_payload["manifest"],
        ]
    )

    assert second == 0
    second_payload = json.loads(capsys.readouterr().out)
    assert second_payload["comparisons"]
    assert all(
        item["divergences"] == [] for item in second_payload["comparisons"]
    )


def test_cli_rejects_non_string_verify_command_tokens(tmp_path, capsys) -> None:
    code = cli.main(
        [
            "collab",
            "--task",
            "task",
            "--verify-command-json",
            '["python", 1]',
            "--out",
            str(tmp_path / "records"),
        ]
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "TypeError"
    assert "每一项都必须是字符串" in payload["message"]
