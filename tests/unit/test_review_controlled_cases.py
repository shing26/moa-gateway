from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bench.harness.runner import load_task
from bench.harness.verify import run_command
from bench.harness.workspace import changed_paths, materialize
from evals.review_value import ROOT, load_controlled_cases

NEW_E3_CASE_IDS = (
    "e3-c4-report-evidence-roundtrip",
    "e3-c5-feishu-event-idempotency",
    "e3-c6-budget-input-guard",
    "e3-c7-feishu-post-fallback",
)


@pytest.mark.parametrize("case_id", NEW_E3_CASE_IDS)
def test_controlled_case_patches_use_lf(case_id: str) -> None:
    case = next(item for item in load_controlled_cases() if item.case_id == case_id)
    task_path = ROOT / case.task_file
    patch_paths = (
        task_path.with_name(f"{task_path.stem.removesuffix('.task')}.test.patch"),
        task_path.with_name(f"{task_path.stem.removesuffix('.task')}.solution.patch"),
    )

    for patch_path in patch_paths:
        assert b"\r\n" not in patch_path.read_bytes(), (
            f"{patch_path.relative_to(ROOT)} must stay LF so git apply matches "
            "the LF workspace produced by git archive"
        )


def _commit_available(sha: str) -> bool:
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


@pytest.mark.parametrize("case_id", NEW_E3_CASE_IDS)
def test_new_controlled_case_fails_first_and_solution_converges(
    tmp_path: Path,
    case_id: str,
) -> None:
    case = next(item for item in load_controlled_cases() if item.case_id == case_id)
    task_path = ROOT / case.task_file
    task = load_task(task_path)
    if not _commit_available(task.base_commit):
        pytest.skip(f"shallow clone: missing {task.base_commit}")

    workspace = materialize(
        repo=ROOT,
        base_commit=task.base_commit,
        test_patch=task.test_patch,
        dest=tmp_path / case_id,
    )
    fail_first = run_command(task.argv, cwd=workspace)
    assert fail_first.exit_code == 1, fail_first.output
    assert task.test_paths[0].split("/")[-1] in fail_first.output

    solution_path = task_path.with_name(
        f"{task_path.stem.removesuffix('.task')}.solution.patch"
    )
    solution = solution_path.read_text(encoding="utf-8")
    assert changed_paths(solution)
    assert all(not path.startswith("tests/") for path in changed_paths(solution))

    applied = subprocess.run(
        ["git", "apply", str(solution_path)],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    verdict = run_command(task.argv, cwd=workspace)
    assert verdict.exit_code == 0, verdict.output
