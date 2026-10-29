"""Issue 分诊任务定义与判定。

与 ``bench/harness/runner.py`` 的代码修复任务平行：issue 文本进、结构化分诊
JSON 出、对着任务目录里的 ground truth 判定。判定不经过 agent —— 与 §7 的
防作弊原则一致，pass/fail 由 harness 给出。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.agent.types import AgentFactory, AgentRun, Budget, CodingAgent


@dataclass(frozen=True)
class TriageTask:
    task_id: str
    issue_text: str
    expected_category: str
    expected_priority: str
    expected_route: str
    allowed_categories: tuple[str, ...]
    allowed_priorities: tuple[str, ...]
    allowed_routes: tuple[str, ...]
    note: str = ""


TRIAGE_CATEGORIES = ("bug", "feature", "question", "docs")
TRIAGE_PRIORITIES = ("P0", "P1", "P2", "P3")
TRIAGE_ROUTES = ("auto", "human")


def load_triage_task(path: Path) -> TriageTask:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = (
        "task_id",
        "issue_text",
        "expected_category",
        "expected_priority",
        "expected_route",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"分诊任务缺少字段: {', '.join(missing)}")
    if not str(payload["task_id"]).strip():
        raise ValueError("task_id 不能为空")
    for key in ("expected_category", "expected_priority", "expected_route"):
        value = str(payload[key])
        allowed = {
            "expected_category": TRIAGE_CATEGORIES,
            "expected_priority": TRIAGE_PRIORITIES,
            "expected_route": TRIAGE_ROUTES,
        }[key]
        if value not in allowed:
            raise ValueError(
                f"{payload['task_id']}.{key} 值 {value!r} 不在闭集内: {allowed}"
            )
    return TriageTask(
        task_id=str(payload["task_id"]),
        issue_text=str(payload["issue_text"]),
        expected_category=str(payload["expected_category"]),
        expected_priority=str(payload["expected_priority"]),
        expected_route=str(payload["expected_route"]),
        allowed_categories=TRIAGE_CATEGORIES,
        allowed_priorities=TRIAGE_PRIORITIES,
        allowed_routes=TRIAGE_ROUTES,
        note=str(payload.get("note", "")),
    )


def _parse_triage_output(text: str) -> dict[str, Any] | None:
    """提取 agent 输出里的第一个 JSON 对象。"""
    for match in re.finditer(r"\{(?:[^{}]|\{[^{}]*\})*\}", text, re.DOTALL):
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def verify_triage_output(text: str, task: TriageTask) -> tuple[bool, dict[str, Any]]:
    """判定分诊输出。category + route 全对为 pass；priority 仅记录不判决。"""
    payload = _parse_triage_output(text)
    if payload is None:
        return False, {"error": "未找到可解析的分诊 JSON"}
    checks = {
        "category": str(payload.get("category", "")) == task.expected_category,
        "priority": str(payload.get("priority", "")) == task.expected_priority,
        "route": str(payload.get("route", "")) == task.expected_route,
    }
    decision_checks = {
        key: value for key, value in checks.items() if key != "priority"
    }
    return all(decision_checks.values()), {
        "parsed": payload,
        "checks": checks,
        "decision_fields": ("category", "route"),
        "recorded_only": ("priority",),
    }


@dataclass(frozen=True)
class TriageResult:
    task_id: str
    arm: str
    base_commit: str
    agent_run: AgentRun
    outcome: str
    patch: str
    fail_first_exit_code: int
    verdict_exit_code: int
    verdict_output: str
    parsed_output: dict[str, Any] | None
    verdict: dict[str, Any]
    wall_clock_s: float
    budget_tier: str = "B1"
    baseline_cost_usd: float = 0.0
    baseline_tokens: int = 0
    patch_touched_tests: bool = False


TRIAGE_GOAL_TEMPLATE = """你是一个 Issue 分诊助手。请阅读下面的 issue 文本，输出一个 JSON 对象作为分诊结论。

闭集定义：
- category: {categories}
- priority: {priorities}
- route: {routes}

Issue 文本：
{issue_text}

要求：
1. 只输出一个 JSON 对象，格式为 {{"category": "...", "priority": "...", "route": "..."}}
2. 不要输出任何其他文字
3. route 为 human 的条件：涉及安全、隐私、资金、数据删除或无法自动归类
4. 你可以使用只读工具（read_file / list_files / search）查看当前仓库以辅助判断"""


async def run_triage_task(
    *,
    task: TriageTask,
    agent_factory: AgentFactory,
    workdir: Path,
    arm: str = "L0",
    budget: Budget,
    timeout_s: float = 300.0,
    budget_tier: str = "B1",
    baseline_cost_usd: float = 0.0,
    baseline_tokens: int = 0,
) -> TriageResult:
    """执行一次分诊 Trial：agent 在只读上下文里工作，harness 判定 JSON。"""
    from bench.agent.tools import build_toolset
    from bench.harness.workspace import materialize

    workspace = workdir / task.task_id
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"分诊工作区非空: {workspace}")
    workspace = materialize(
        repo=_repo_root(),
        base_commit="HEAD",
        test_patch=_empty_patch(),
        dest=workspace,
        allow_empty_patch=True,
    )
    tools = build_toolset(
        workspace=workspace,
        verify_command=(),
        names=("read_file", "list_files", "search"),
    )
    agent: CodingAgent = agent_factory(workspace=workspace, tools=tools)
    goal = TRIAGE_GOAL_TEMPLATE.format(
        categories=", ".join(task.allowed_categories),
        priorities=", ".join(task.allowed_priorities),
        routes=", ".join(task.allowed_routes),
        issue_text=task.issue_text,
    )
    started = time.monotonic()
    agent_run = await agent.run(workspace=workspace, goal=goal, budget=budget)
    wall_clock_s = time.monotonic() - started
    final_text = agent_run.stop_reason
    # agent 的最终文本在 finish 步骤的 observation 里；stop_reason 只是停机原因
    finish_steps = [s for s in agent_run.steps if s.action == "finish"]
    if finish_steps and finish_steps[-1].observation:
        final_text = finish_steps[-1].observation
    passed, verdict = verify_triage_output(final_text, task)
    outcome = agent_run.status if agent_run.status in ("budget_exhausted", "error") else ("pass" if passed else "fail")
    return TriageResult(
        task_id=task.task_id,
        arm=arm,
        base_commit="",
        patch="",
        fail_first_exit_code=-1,
        verdict_exit_code=0 if passed else 1,
        verdict_output=json.dumps(verdict, ensure_ascii=False),
        agent_run=agent_run,
        outcome=outcome,
        parsed_output=verdict.get("parsed"),
        verdict=verdict,
        wall_clock_s=wall_clock_s,
        budget_tier=budget_tier,
        baseline_cost_usd=baseline_cost_usd,
        baseline_tokens=baseline_tokens,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _empty_patch() -> Path:
    path = Path(__file__).with_name("_empty.patch")
    path.write_text("", encoding="utf-8")
    return path
