"""harness：orphan 快照、fail-first、还原、patch 采集，以及端到端验收。

端到端那两条会真的物化工作区、真的跑 pytest（约几秒），并且**只在完整克隆下**才
有意义——浅克隆拿不到基线提交，那时它们会 skip。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import fields, replace
from pathlib import Path

import pytest

from bench.agent.loop import LLMBackedAgent
from bench.agent.scripted import ScriptedLLM, ScriptedTurn
from bench.agent.types import AgentRun, Budget, RunResult
from bench.arms import resolve_arm
from bench.harness.runner import (
    InvalidTaskError,
    TaskSpec,
    load_task,
    run_task,
    write_result,
)
from bench.harness.verify import run_command
from bench.harness.workspace import (
    changed_paths,
    collect_patch,
    git,
    has_original_history,
    materialize,
    restore_test_files,
    touched_test_paths,
)

ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = ROOT / "bench" / "tasks" / "988fccb.task.json"
#: 修复提交与它的父提交。父提交是基线，修复内容按 tests/ 拆成两个 patch。
FIX_COMMIT = "988fccbb242be0573f4e42f05118276884272bf3"
FIXED_FILE = "app/middleware/auth.py"


def _commit_available(sha: str) -> bool:
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", sha], capture_output=True, check=False
    )
    return probe.returncode == 0


def _show(pathspec: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", pathspec],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


requires_history = pytest.mark.skipif(
    not _commit_available(FIX_COMMIT),
    reason="需要完整克隆：浅克隆里没有基线提交",
)


def spec():
    return load_task(TASK_FILE)


@requires_history
def test_materialize_leaves_no_original_history(tmp_path: Path) -> None:
    task = spec()
    workspace = materialize(
        repo=ROOT,
        base_commit=task.base_commit,
        test_patch=task.test_patch,
        dest=tmp_path / "ws",
    )
    assert has_original_history(workspace, task.base_commit) is False
    assert git(["rev-list", "--count", "HEAD"], cwd=workspace).stdout.strip() == "1"
    # 判据（test patch）已经在工作区里了——否则 fail-first 无从谈起。
    content = (workspace / "tests/unit/test_auth.py").read_text(encoding="utf-8")
    assert "test_knowledge_and_privacy_routes_fail_closed" in content
    # 而修复不在。
    assert "_DASH_PROTECTED_PREFIXES" not in (
        workspace / FIXED_FILE
    ).read_text(encoding="utf-8")


@requires_history
def test_restore_test_files_undoes_edits_and_new_files(tmp_path: Path) -> None:
    task = spec()
    workspace = materialize(
        repo=ROOT,
        base_commit=task.base_commit,
        test_patch=task.test_patch,
        dest=tmp_path / "ws",
    )
    # tests/conftest.py 在基线里就是**已跟踪**文件：agent 改它是"修改"而非"新增"，
    # 只按 test_paths 还原的话这条篡改会被放过。
    original_conftest = (workspace / "tests/conftest.py").read_text(encoding="utf-8")
    (workspace / "tests/unit/test_auth.py").write_text(
        "def test_trivial():\n    pass\n", encoding="utf-8", newline="\n"
    )
    (workspace / "tests/unit/sneaky_test.py").write_text(
        "def test_ok():\n    pass\n", encoding="utf-8", newline="\n"
    )
    # 测试文件**父目录之外**的新增文件（测试树根一级）同样要清掉。
    (workspace / "tests/sneaky_root_test.py").write_text(
        "def test_ok():\n    pass\n", encoding="utf-8", newline="\n"
    )
    (workspace / "tests/conftest.py").write_text("# 篡改收集钩子\n", encoding="utf-8", newline="\n")
    restore_test_files(workspace, task.test_paths)
    assert "test_knowledge_and_privacy_routes_fail_closed" in (
        workspace / "tests/unit/test_auth.py"
    ).read_text(encoding="utf-8")
    assert not (workspace / "tests/unit/sneaky_test.py").exists()
    assert not (workspace / "tests/sneaky_root_test.py").exists()
    assert (workspace / "tests/conftest.py").read_text(encoding="utf-8") == original_conftest


@requires_history
def test_patch_collection_records_agent_edits_and_flags_test_touch(
    tmp_path: Path,
) -> None:
    task = spec()
    workspace = materialize(
        repo=ROOT,
        base_commit=task.base_commit,
        test_patch=task.test_patch,
        dest=tmp_path / "ws",
    )
    (workspace / FIXED_FILE).write_text("# touched\n", encoding="utf-8", newline="\n")
    (workspace / "tests/unit/test_auth.py").write_text(
        "# also touched\n", encoding="utf-8", newline="\n"
    )
    patch = collect_patch(workspace)
    assert FIXED_FILE in patch
    assert touched_test_paths(patch, task.test_paths) is True


@pytest.mark.asyncio
@requires_history
async def test_fail_first_rejects_a_task_that_already_passes(tmp_path: Path) -> None:
    task = replace(spec(), verify_command=("{python}", "-c", "pass"))

    def factory(*, workspace: Path, tools) -> LLMBackedAgent:
        return LLMBackedAgent(ScriptedLLM([ScriptedTurn(final="")]), tools)

    with pytest.raises(InvalidTaskError):
        await run_task(
            spec=task,
            agent_factory=factory,
            repo=ROOT,
            workdir=tmp_path / "work",
            budget=Budget(max_cost_usd=1.0, max_steps=5),
        )


@pytest.mark.asyncio
@requires_history
async def test_end_to_end_real_fix_passes(tmp_path: Path) -> None:
    """scripted agent 写入真修复 → 判决 pass。"""
    fixed_content = _show(f"{FIX_COMMIT}:{FIXED_FILE}")

    def factory(*, workspace: Path, tools) -> LLMBackedAgent:
        turns = [
            ScriptedTurn(
                tool="write_file",
                arguments={"path": FIXED_FILE, "content": fixed_content},
                cost_usd=0.001,
            ),
            ScriptedTurn(final="已修好", cost_usd=0.001),
        ]
        return LLMBackedAgent(ScriptedLLM(turns), tools)

    result = await run_task(
        spec=spec(),
        agent_factory=factory,
        repo=ROOT,
        workdir=tmp_path / "work",
        budget=Budget(max_cost_usd=1.0, max_steps=10),
    )
    assert result.outcome == "pass", result.verdict_output
    assert result.agent_run.status == "completed"
    assert result.fail_first_exit_code != 0
    assert result.patch_touched_tests is False
    assert "_DASH_PROTECTED_PREFIXES" in result.patch
    assert write_result(result, tmp_path / "result.json").exists()


@pytest.mark.asyncio
@requires_history
async def test_registry_l0_end_to_end_lands_a_full_run_result(tmp_path: Path) -> None:
    """档位按名字从注册表取到，一路跑到判决，并落一份字段齐全的 RunResult JSON。"""
    fixed_content = _show(f"{FIX_COMMIT}:{FIXED_FILE}")
    factory = resolve_arm(
        "L0",
        llm=ScriptedLLM(
            [
                ScriptedTurn(
                    tool="write_file",
                    arguments={"path": FIXED_FILE, "content": fixed_content},
                    cost_usd=0.001,
                ),
                ScriptedTurn(final="已修好", cost_usd=0.001),
            ]
        ),
    )

    result = await run_task(
        spec=spec(),
        agent_factory=factory,
        repo=ROOT,
        workdir=tmp_path / "work",
        arm="L0",
        budget=Budget(max_cost_usd=1.0, max_steps=10),
    )

    assert result.outcome == "pass", result.verdict_output
    out = write_result(result, tmp_path / "result.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    # "完整"是可核的：判决与自报两个类型声明的字段一个不缺。
    assert set(payload) == {field.name for field in fields(RunResult)}
    assert set(payload["agent_run"]) == {field.name for field in fields(AgentRun)}
    assert payload["outcome"] == "pass"
    assert payload["arm"] == "L0"
    assert payload["agent_run"]["status"] == "completed"


@pytest.mark.asyncio
@requires_history
async def test_end_to_end_only_editing_tests_fails(tmp_path: Path) -> None:
    """只改测试文件的"修复"必须被判 fail：还原之后判据还在。"""

    def factory(*, workspace: Path, tools) -> LLMBackedAgent:
        turns = [
            ScriptedTurn(
                tool="write_file",
                arguments={
                    "path": "tests/unit/test_auth.py",
                    "content": "def test_knowledge_and_privacy_routes_fail_closed():\n    pass\n",
                },
                cost_usd=0.001,
            ),
            ScriptedTurn(final="改好了", cost_usd=0.001),
        ]
        return LLMBackedAgent(ScriptedLLM(turns), tools)

    result = await run_task(
        spec=spec(),
        agent_factory=factory,
        repo=ROOT,
        workdir=tmp_path / "work",
        budget=Budget(max_cost_usd=1.0, max_steps=10),
    )
    assert result.outcome == "fail"
    assert result.patch_touched_tests is True


# ── 其余 E1 任务（票 01：任务集扩容到易 / 中 / 难三档）────────────────────────

#: task_id → 修复 commit。task.json 里的 base_commit 是它的父提交；两个 patch 由该
#: commit 按 tests/ 路径拆开，运行期不再读原始 commit。
EXTRA_FIX_COMMITS = {
    "a746b81": "a746b816bca7fbce6911d7133bb051bc66e7ea9d",
    "0896629": "0896629257aa5c282d9e54460c5b1b3ff88e0192",
    "bd4f186": "bd4f186522a5b23a88cbf70304f8bac699b9311b",
    "60c64ed": "60c64edb7987379cda18e9a4ff9862b939ef8333",
}
EXTRA_TASK_IDS = sorted(EXTRA_FIX_COMMITS)

#: 每条任务 fail-first 必须出现的失败特征。只判"退出码非零"挡不住假阳性（导入错误、
#: 收集错误也非零），也不足以说明失败落在修复面附近——这里把每条任务的预期失败原因
#: 钉住，读的是验证输出本身（handoff 条款 2）。
EXTRA_FAIL_FIRST_MARKERS = {
    "a746b81": ("assert True is False",),
    "0896629": ("assert None ==",),
    "bd4f186": ("AttributeError", "_WRITER_ID"),
    "60c64ed": ("assert 0 == 1",),
}


def extra_spec(task_id: str) -> TaskSpec:
    return load_task(ROOT / "bench" / "tasks" / f"{task_id}.task.json")


def _extra_task_available(task_id: str) -> bool:
    """完整克隆才有基线提交；浅克隆下这些用例应当 skip，而不是物化失败。"""
    return _commit_available(EXTRA_FIX_COMMITS[task_id]) and _commit_available(
        extra_spec(task_id).base_commit
    )


EXTRA_TASK_PARAMS = [
    pytest.param(
        task_id,
        marks=pytest.mark.skipif(
            not _extra_task_available(task_id),
            reason=f"需要完整克隆：浅克隆里没有 {task_id} 的基线提交",
        ),
    )
    for task_id in EXTRA_TASK_IDS
]


def _solution_files(task_id: str) -> list[str]:
    """solution patch 改过的文件路径（四条候选都在改既有文件，没有新增/删除）。"""
    patch = (ROOT / "bench" / "tasks" / f"{task_id}.solution.patch").read_text(
        encoding="utf-8"
    )
    return changed_paths(patch)


def _pinned_nodeid(task: TaskSpec) -> str:
    return next(token for token in task.verify_command if "::" in token)


@pytest.mark.parametrize("task_id", EXTRA_TASK_PARAMS)
def test_extra_tasks_fail_first_fails_on_the_fix_surface(
    tmp_path: Path, task_id: str
) -> None:
    """基线上任务必须失败，且失败的是钉住的那条判据——不是收集错误。

    pytest 退出码 1 = 有测试失败；收集错误是 2、一条都没收集到是 5。再要求失败摘要里
    出现钉住的 nodeid，才挡得住"import 挂了也算 fail-first"这条假阳性。
    """
    task = extra_spec(task_id)
    workspace = materialize(
        repo=ROOT,
        base_commit=task.base_commit,
        test_patch=task.test_patch,
        dest=tmp_path / task_id,
    )
    # solution patch 必须能应用到 git archive 出来的基线（handoff 条款 5）。这里顺带
    # 检，是因为工作区已经物化好了，git apply --check 几乎不花时间。
    solution_patch = ROOT / "bench" / "tasks" / f"{task_id}.solution.patch"
    applied = git(["apply", "--check", str(solution_patch)], cwd=workspace, check=False)
    assert applied.returncode == 0, applied.stderr
    result = run_command(task.argv, cwd=workspace)
    assert result.exit_code == 1, result.output
    assert f"FAILED {_pinned_nodeid(task)}" in result.output
    missing = [m for m in EXTRA_FAIL_FIRST_MARKERS[task_id] if m not in result.output]
    assert not missing, f"fail-first 失败特征缺失 {missing}：\n{result.output}"


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", EXTRA_TASK_PARAMS)
async def test_extra_tasks_end_to_end_real_fix_passes(
    tmp_path: Path, task_id: str
) -> None:
    """scripted agent 按 solution patch 写回全部文件 → 判决 pass。"""
    task = extra_spec(task_id)
    commit = EXTRA_FIX_COMMITS[task_id]
    files = _solution_files(task_id)
    contents = {path: _show(f"{commit}:{path}") for path in files}

    def factory(*, workspace: Path, tools) -> LLMBackedAgent:
        turns = [
            ScriptedTurn(
                tool="write_file",
                arguments={"path": path, "content": contents[path]},
                cost_usd=0.001,
            )
            for path in files
        ]
        turns.append(ScriptedTurn(final="已修好", cost_usd=0.001))
        return LLMBackedAgent(ScriptedLLM(turns), tools)

    result = await run_task(
        spec=task,
        agent_factory=factory,
        repo=ROOT,
        workdir=tmp_path / "work",
        budget=Budget(max_cost_usd=1.0, max_steps=10),
    )
    assert result.outcome == "pass", result.verdict_output
    assert result.agent_run.status == "completed"
    assert result.fail_first_exit_code != 0
    assert result.patch_touched_tests is False


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", EXTRA_TASK_PARAMS)
async def test_extra_tasks_end_to_end_only_editing_tests_fails(
    tmp_path: Path, task_id: str
) -> None:
    """只改测试文件的"修复"必须被判 fail：还原之后判据还在。"""
    task = extra_spec(task_id)
    target = task.test_paths[0]

    def factory(*, workspace: Path, tools) -> LLMBackedAgent:
        turns = [
            ScriptedTurn(
                tool="write_file",
                arguments={
                    "path": target,
                    "content": "def test_trivial():\n    pass\n",
                },
                cost_usd=0.001,
            ),
            ScriptedTurn(final="改好了", cost_usd=0.001),
        ]
        return LLMBackedAgent(ScriptedLLM(turns), tools)

    result = await run_task(
        spec=task,
        agent_factory=factory,
        repo=ROOT,
        workdir=tmp_path / "work",
        budget=Budget(max_cost_usd=1.0, max_steps=10),
    )
    assert result.outcome == "fail"
    assert result.patch_touched_tests is True
