"""跑一个任务：物化 → fail-first → 跑 agent → 采集 patch → 还原 → 验证 → RunResult。"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.agent.types import (
    AgentFactory,
    AgentRun,
    Budget,
    CodingAgent,
    E1_ORIGIN,
    Outcome,
    RunResult,
    TaskOrigin,
)
from bench.agent.tools import build_toolset
from bench.harness.verify import run_command
from bench.harness.workspace import (
    collect_patch,
    materialize,
    restore_test_files,
    touched_test_paths,
)

logger = logging.getLogger("bench.harness.runner")

#: 只给中性目标。原始的 issue / commit message 会直接写着怎么修（§7.6 因为同样的
#: 理由剥掉了 .git），而"从失败输出里定位问题"正是这个实验要测的能力。
NEUTRAL_GOAL = "让本任务的验证命令通过。"

#: 任务定义里用 ``{python}`` 表示"跑 bench 的那个解释器"，好让任务文件跨机器可用。
_PYTHON_PLACEHOLDER = "{python}"


class InvalidTaskError(RuntimeError):
    """fail-first 没失败：任务本身是坏的（代码没坏 / 测试没覆盖），必须剔除。"""


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    base_commit: str
    verify_command: tuple[str, ...]
    test_paths: tuple[str, ...]
    test_patch: Path
    difficulty: str = ""
    source: TaskOrigin = E1_ORIGIN

    @property
    def argv(self) -> tuple[str, ...]:
        return tuple(
            sys.executable if token == _PYTHON_PLACEHOLDER else token
            for token in self.verify_command
        )


def load_task(path: Path) -> TaskSpec:
    payload = json.loads(path.read_text(encoding="utf-8"))
    task_id = str(payload["task_id"])
    source = str(payload.get("source", E1_ORIGIN))
    if source not in ("E1", "E3"):
        # 来源是闭集；收下一个不认识的来源会让它从报告里"消失"又不报错——宁可炸。
        raise ValueError(f"{task_id}: 未知任务来源 {source!r}（只认 E1 / E3）")
    return TaskSpec(
        task_id=task_id,
        base_commit=str(payload["base_commit"]),
        verify_command=tuple(str(item) for item in payload["verify_command"]),
        test_paths=tuple(str(item) for item in payload.get("test_paths", ())),
        test_patch=path.parent / f"{task_id}.test.patch",
        difficulty=str(payload.get("difficulty", "")),
        source=source,  # type: ignore[arg-type]
    )


async def run_task(
    *,
    spec: TaskSpec,
    agent_factory: AgentFactory,
    repo: Path,
    workdir: Path,
    arm: str = "L0",
    goal: str = NEUTRAL_GOAL,
    budget: Budget,
    timeout_s: float = 300.0,
    budget_tier: str = "B1",
    baseline_cost_usd: float = 0.0,
) -> RunResult:
    workspace = materialize(
        repo=repo,
        base_commit=spec.base_commit,
        test_patch=spec.test_patch,
        dest=workdir / spec.task_id,
    )

    fail_first = run_command(spec.argv, cwd=workspace, timeout_s=timeout_s)
    if fail_first.passed:
        # §7.1：不失败说明任务本身是坏的，后面所有数字都可能是"本来就是绿的"。
        raise InvalidTaskError(
            f"{spec.task_id}: fail-first 期望失败但通过了（任务无效）"
        )

    tools = build_toolset(workspace=workspace, verify_command=spec.argv)
    agent: CodingAgent = agent_factory(workspace=workspace, tools=tools)

    started = time.monotonic()
    agent_run: AgentRun = await agent.run(
        workspace=workspace, goal=goal, budget=budget
    )
    wall_clock_s = time.monotonic() - started

    # 顺序是刻意的：**先**采集 patch（含 agent 对测试文件的改动，那是作弊的证据），
    # **再**还原测试文件，**最后**验证。反过来就把证据洗掉了。
    patch = collect_patch(workspace)
    patch_touched_tests = touched_test_paths(patch, spec.test_paths)
    restore_test_files(workspace, spec.test_paths)

    verdict = run_command(spec.argv, cwd=workspace, timeout_s=timeout_s)
    outcome = _outcome(agent_run, verdict.passed)

    return RunResult(
        task_id=spec.task_id,
        arm=arm,
        base_commit=spec.base_commit,
        agent_run=agent_run,
        outcome=outcome,
        patch=patch,
        fail_first_exit_code=fail_first.exit_code,
        verdict_exit_code=verdict.exit_code,
        verdict_output=verdict.output,
        wall_clock_s=wall_clock_s,
        patch_touched_tests=patch_touched_tests,
        budget_tier=budget_tier,
        baseline_cost_usd=baseline_cost_usd,
        source=spec.source,
    )


def _outcome(agent_run: AgentRun, passed: bool) -> Outcome:
    """判决。``budget_exhausted`` / ``error`` 直接透传——§8 要求它们与 ``fail`` 分开，
    否则"协作太贵"会被误读成"协作不行"。"""
    if agent_run.status == "budget_exhausted":
        return "budget_exhausted"
    if agent_run.status == "error":
        return "error"
    return "pass" if passed else "fail"


def write_result(result: RunResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def summarize(result: RunResult) -> str:
    """一行摘要，给人看。"""
    usage: Any = result.agent_run.usage
    return (
        f"{result.task_id} source={result.source} arm={result.arm} "
        f"tier={result.budget_tier} "
        f"outcome={result.outcome} "
        f"status={result.agent_run.status} steps={len(result.agent_run.steps)} "
        f"cost=${usage.cost_usd:.6f} calls={usage.llm_calls} "
        f"wall={result.wall_clock_s:.1f}s tests_touched={result.patch_touched_tests}"
    )


__all__ = [
    "NEUTRAL_GOAL",
    "InvalidTaskError",
    "TaskSpec",
    "load_task",
    "run_task",
    "summarize",
    "write_result",
]
