from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from apps.code_review_pipeline.change import (
    ChangePipeline,
    ChangeRequest,
    ChangeStatus,
)
from apps.code_review_pipeline.change import gates as gates_module
from apps.code_review_pipeline.change.gates import GateReport, forced_lint_gate

# 注意：test_strength_gate 以模块属性调用——直接 import 会被 pytest 当成测试收集
strength_gate = gates_module.test_strength_gate


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "init"], root)
    return root


def _request() -> ChangeRequest:
    return ChangeRequest(
        repo="owner/name",
        base_sha="a" * 40,
        title="fix: correct add",
        body="agent change",
        goal="fix add",
        branch="moa/change-1",
        trace_id="trace-1",
    )


def _passing() -> tuple[str, ...]:
    return (sys.executable, "-c", "raise SystemExit(0)")


# ---- forced_lint_gate ----


def test_lint_gate_rejects_unused_import_in_added_file(repo: Path) -> None:
    (repo / "new_module.py").write_text("import os\n\nVALUE = 1\n", encoding="utf-8")

    report = forced_lint_gate(repo, added=["new_module.py"], modified=[])

    assert report.status == "failed"
    assert len(report.violations) == 1
    assert report.violations[0].path == "new_module.py"
    assert "F401" in report.violations[0].detail
    assert report.violations[0].line == 1


def test_lint_gate_base_control_tolerates_pre_existing_finding(repo: Path) -> None:
    legacy = repo / "legacy.py"
    legacy.write_text("import os\n\nVALUE = 1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "add legacy"], repo)

    # 工作版本保留同一个未使用 import，只改动其他内容——基线对比应放过旧问题
    legacy.write_text("import os\n\nVALUE = 2\n", encoding="utf-8")

    report = forced_lint_gate(repo, added=[], modified=["legacy.py"])

    assert report.status == "passed"
    assert report.violations == ()


def test_lint_gate_flags_new_f841_in_modified_file(repo: Path) -> None:
    legacy = repo / "legacy.py"
    legacy.write_text("import os\n\nVALUE = 1\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "add legacy"], repo)

    legacy.write_text(
        "import os\n\nVALUE = 1\n\n\ndef later():\n    unused = 5\n    return None\n",
        encoding="utf-8",
    )

    report = forced_lint_gate(repo, added=[], modified=["legacy.py"])

    assert report.status == "failed"
    assert any(v.path == "legacy.py" and "F841" in v.detail for v in report.violations)
    # HEAD 已有的 F401 不应被重复上报
    assert not any("F401" in v.detail for v in report.violations)


def test_lint_gate_skips_when_ruff_is_missing(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = importlib.util.find_spec

    def _no_ruff(name: str):
        return None if name == "ruff" else real_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", _no_ruff)
    (repo / "new_module.py").write_text("import os\n\nVALUE = 1\n", encoding="utf-8")

    report = forced_lint_gate(repo, added=["new_module.py"], modified=[])

    assert report.status == "skipped"
    assert "ruff" in report.skipped_reason
    # skip 必须机器可读地进 audit：to_dict 要保留 reason
    assert report.to_dict()["skipped_reason"] == report.skipped_reason
    assert report.to_dict()["violations"] == []


def test_lint_gate_skips_on_unexpected_ruff_exit_code(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object):
        return subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr="ruff exploded"
        )

    monkeypatch.setattr(gates_module.subprocess, "run", fake_run)
    (repo / "new_module.py").write_text("import os\n\nVALUE = 1\n", encoding="utf-8")

    report = forced_lint_gate(repo, added=["new_module.py"], modified=[])

    assert report.status == "skipped"
    assert "exit code 2" in report.skipped_reason
    assert "ruff exploded" in report.skipped_reason


def test_lint_gate_reports_noop_for_clean_python(repo: Path) -> None:
    (repo / "clean.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    report = forced_lint_gate(repo, added=["clean.py"], modified=[])

    assert report.status == "passed"
    assert report.violations == ()


# ---- test_strength_gate ----


def test_strength_gate_rejects_test_without_assert_or_raises(repo: Path) -> None:
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_vacuous.py").write_text(
        "def test_no_assert():\n    computed = 1 + 1\n    return computed\n",
        encoding="utf-8",
    )

    report = strength_gate(
        added=["tests/test_vacuous.py"], modified=[], workspace=repo
    )

    assert report.status == "failed"
    assert len(report.violations) == 1
    assert report.violations[0].path == "tests/test_vacuous.py"
    assert report.violations[0].detail == "test_no_assert"


def test_strength_gate_rejects_vacuous_method_with_qualname(repo: Path) -> None:
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_grouped.py").write_text(
        "class TestGroup:\n    def test_silent(self):\n        pass\n",
        encoding="utf-8",
    )

    report = strength_gate(
        added=["tests/test_grouped.py"], modified=[], workspace=repo
    )

    assert report.status == "failed"
    assert report.violations[0].detail == "TestGroup.test_silent"


def test_strength_gate_accepts_pytest_raises_and_assert_raises(repo: Path) -> None:
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_strong.py").write_text(
        "import pytest\n"
        "\n"
        "\n"
        "def test_with_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        int('x')\n"
        "\n"
        "\n"
        "class TestGroup:\n"
        "    def test_with_unittest_raises(self):\n"
        "        with self.assertRaises(ValueError):\n"
        "            int('x')\n",
        encoding="utf-8",
    )

    report = strength_gate(
        added=["tests/test_strong.py"], modified=[], workspace=repo
    )

    assert report.status == "passed"
    assert report.violations == ()


def test_strength_gate_flags_unparseable_test_file(repo: Path) -> None:
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_broken.py").write_text("def test_broken(:\n", encoding="utf-8")

    report = strength_gate(
        added=["tests/test_broken.py"], modified=[], workspace=repo
    )

    assert report.status == "failed"
    assert report.violations[0].path == "tests/test_broken.py"
    assert "failed to parse" in report.violations[0].detail


def test_strength_gate_ignores_non_test_sources(repo: Path) -> None:
    (repo / "plain.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    report = strength_gate(added=["plain.py"], modified=[], workspace=repo)

    assert report.status == "skipped"
    assert report.skipped_reason == "no test files changed"


# ---- prepare_bundle 集成 ----


def test_prepare_bundle_gate_fails_on_vacuous_test_file(repo: Path) -> None:
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_generated.py").write_text(
        "def test_generated():\n    pass\n", encoding="utf-8"
    )
    pipeline = ChangePipeline(
        github=object(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
    )

    result = asyncio.run(pipeline.prepare_bundle(_request()))

    assert result.status is ChangeStatus.GATE_FAILED
    assert result.ok is False
    assert result.bundle is not None  # 证据仍然保留
    gates_audit = result.audit["gates"]
    assert {item["name"] for item in gates_audit} == {"forced_lint", "test_strength"}
    strength = next(item for item in gates_audit if item["name"] == "test_strength")
    assert strength["status"] == "failed"
    assert "tests/test_generated.py:test_generated" in result.message


def test_prepare_bundle_clean_change_passes_gates_and_audits(repo: Path) -> None:
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    pipeline = ChangePipeline(
        github=object(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
    )

    result = asyncio.run(pipeline.prepare_bundle(_request()))

    assert result.status is ChangeStatus.VERIFIED
    gates_audit = result.audit["gates"]
    assert {item["name"] for item in gates_audit} == {"forced_lint", "test_strength"}
    lint = next(item for item in gates_audit if item["name"] == "forced_lint")
    strength = next(item for item in gates_audit if item["name"] == "test_strength")
    assert lint["status"] == "passed"
    # 只改了源码：测试强度门禁如实上报 skip，不冒充通过
    assert strength["status"] == "skipped"
    assert strength["skipped_reason"] == "no test files changed"


def test_prepare_bundle_non_python_change_skips_both_gates(repo: Path) -> None:
    (repo / "notes.txt").write_text("docs only\n", encoding="utf-8")
    pipeline = ChangePipeline(
        github=object(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
    )

    result = asyncio.run(pipeline.prepare_bundle(_request()))

    assert result.status is ChangeStatus.VERIFIED
    gates_audit = result.audit["gates"]
    assert len(gates_audit) == 2
    assert all(item["status"] == "skipped" for item in gates_audit)
    assert all(
        item["skipped_reason"] == "no python files changed" for item in gates_audit
    )


def test_prepare_bundle_audits_ruff_missing_skip(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = importlib.util.find_spec

    def _no_ruff(name: str):
        return None if name == "ruff" else real_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", _no_ruff)
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    pipeline = ChangePipeline(
        github=object(),
        repo="owner/name",
        workspace=repo,
        verify_command=_passing(),
        model_name="test-model",
    )

    result = asyncio.run(pipeline.prepare_bundle(_request()))

    assert result.status is ChangeStatus.VERIFIED
    lint = next(item for item in result.audit["gates"] if item["name"] == "forced_lint")
    assert lint["status"] == "skipped"
    assert "ruff" in lint["skipped_reason"]


def test_gate_report_dict_shape_defaults() -> None:
    report = GateReport(name="forced_lint", status="passed")

    assert report.violations == ()
    assert report.skipped_reason == ""
    assert report.to_dict() == {
        "name": "forced_lint",
        "status": "passed",
        "violations": [],
        "skipped_reason": "",
    }
