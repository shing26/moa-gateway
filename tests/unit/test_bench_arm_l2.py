"""L2 档：Planner + 1 worker + Critic（§5 的四档表）。

这一档要证明的是**交接**：规划出的子任务自带上下文，执行者不必回头问；worker 的
历史不跨子任务，所以"下一条子任务拿到的"必须来自显式交接，而不是同一条会话顺手
带过去。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.agents.provider import ChatResult
from app.agents.tools import ToolRegistry

from bench.agent.expert import ExpertPool, ToolExpert
from bench.agent.scripted import ScriptedTurn
from bench.agent.tools import build_toolset
from bench.agent.types import Budget
from bench.arms import arm_names, resolve_arm
from bench.arms.l2 import L2PlannerWorkerCritic, PlanPolicy, build_l2
from bench.harness.runner import TaskSpec, load_task, run_task

ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = ROOT / "bench" / "tasks" / "988fccb.task.json"
PASSING_VERIFY = (sys.executable, "-c", "pass")


class _ScriptLLM:
    """按 messages 分派 planner / worker / Critic 三段的回包。

    三段在真机上都是同一模型的提示，离线靠 system prompt 与 user 内容区分；每次调用
    都记下 messages，好断言"交接内容真的进了 worker 的上下文"。
    """

    def __init__(self, script: Any, *, cost_usd: float = 0.001) -> None:
        self._script = script
        self._cost = cost_usd
        self.config = type("_Cfg", (), {"model": "scripted", "temperature": 0.0})()
        self.last_metrics: dict[str, Any] = {}
        self.seen: list[list[dict[str, Any]]] = []

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatResult:
        self.seen.append([dict(message) for message in messages])
        self.last_metrics = {
            "model_used": self.config.model,
            "cost_usd": self._cost,
            "llm_latency_ms": 0.0,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
        turn = self._script(messages)
        if turn.tool:
            call = {
                "id": f"call-{len(self.seen)}",
                "type": "function",
                "function": {
                    "name": turn.tool,
                    "arguments": json.dumps(turn.arguments, ensure_ascii=False),
                },
            }
            return ChatResult(messages=[*messages], content="", tool_calls=[call])
        return ChatResult(messages=[*messages], content=turn.final, tool_calls=[])


def _system_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "system"
    )


def _user_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "user"
    )


def _route(messages: list[dict[str, Any]]) -> str:
    system = _system_text(messages)
    user = _user_text(messages)
    if "审查专家" in system:
        return "critic"
    if "只回一个 JSON 数组" in user:
        return "planner"
    return "worker"


def _plan_json(*instructions: str) -> str:
    subtasks = [
        {"instruction": instruction, "context": f"关于「{instruction}」的背景事实"}
        for instruction in instructions
    ]
    return "```json\n" + json.dumps({"subtasks": subtasks}, ensure_ascii=False) + "\n```"


def _critic_json(approved: bool, instructions: str = "") -> str:
    payload: dict[str, Any] = {"verdict": "approve" if approved else "revise"}
    if not approved:
        payload["instructions"] = instructions
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


class _Worker:
    """离线 worker：每条子任务"写文件 → 自报完成"两轮。

    ``LoopSession`` 一次调用只做一件事，所以一个子任务需要两次调用：先写、再宣布
    完成。这里按"你负责的子任务 N"认出是第几条；如果交接内容里带着 Critic 的打回
    理由，就用 ``revised`` 里对应那份内容（模拟"被打回后换一版"）。
    """

    def __init__(
        self,
        contents: list[str],
        *,
        path: str = "note.txt",
        revised: dict[int, str] | None = None,
    ) -> None:
        self._contents = contents
        self._path = path
        self._revised = revised or {}
        self._pending: str | None = None
        self.prompts: list[str] = []

    def turn(self, messages: list[dict[str, Any]]) -> ScriptedTurn:
        text = _user_text(messages)
        self.prompts.append(text)
        if self._pending is None:
            index = self._subtask_index(text)
            content = "?"
            if 0 <= index < len(self._contents):
                content = self._contents[index]
            if index in self._revised and "打回" in text:
                content = self._revised[index]
            self._pending = content
            return ScriptedTurn(
                tool="write_file",
                arguments={"path": self._path, "content": self._pending},
            )
        done = self._pending
        self._pending = None
        return ScriptedTurn(final=f"已写入 {done[:12]}")

    @staticmethod
    def _subtask_index(text: str) -> int:
        marker = "你负责的子任务 "
        if marker not in text:
            return -1
        rest = text.split(marker, 1)[1]
        head = rest.split("：", 1)[0].strip()
        return int(head) if head.isdigit() else -1


def test_l2_is_registered() -> None:
    assert "L2" in arm_names()


def test_l2_critic_is_the_review_expert(tmp_path: Path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    agent = build_l2(llm=_ScriptLLM(lambda m: ScriptedTurn(final="")), workspace=tmp_path, tools=tools)
    assert isinstance(agent, L2PlannerWorkerCritic)
    assert isinstance(agent._critic, ToolExpert)
    assert agent._critic.key == "review"


@pytest.mark.asyncio
async def test_each_subtask_is_handed_off_with_its_context_in_order(tmp_path: Path) -> None:
    """两个子任务按序交接，各自拿到 planner 附的 ``context``。"""
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    worker = _Worker(["A", "B"])

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("第一步：写 A", "第二步：写 B"))
        if route == "critic":
            return ScriptedTurn(final=_critic_json(True))
        return worker.turn(messages)

    agent = L2PlannerWorkerCritic(
        pool=ExpertPool(llm=_ScriptLLM(script), tools=tools),
        workspace=tmp_path,
        policy=PlanPolicy(max_subtasks=4),
    )
    run = await agent.run(workspace=tmp_path, goal="让验证通过", budget=Budget(5.0, 30))

    assert run.status == "completed"
    assert run.subtask_count == 2
    assert run.review_rounds == 0
    # 每条子任务两轮（写 + 完成），两条共四次 worker 调用。
    assert len(worker.prompts) == 4
    first = worker.prompts[0]
    second = next(prompt for prompt in worker.prompts if "你负责的子任务 1：" in prompt)
    assert "第一步：写 A" in first
    assert "关于「第一步：写 A」的背景事实" in first
    assert "第二步：写 B" in second
    assert "关于「第二步：写 B」的背景事实" in second


@pytest.mark.asyncio
async def test_next_subtask_sees_the_previous_result(tmp_path: Path) -> None:
    """handoff 的核心断言：后一个 worker 看得到前一个的产出，不必回头问。"""
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    worker = _Worker(["ALPHA", "ALPHA-BETA"])

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("产出 ALPHA", "在 ALPHA 之上产出 BETA"))
        if route == "critic":
            return ScriptedTurn(final=_critic_json(True))
        return worker.turn(messages)

    agent = build_l2(llm=_ScriptLLM(script), workspace=tmp_path, tools=tools)
    run = await agent.run(workspace=tmp_path, goal="让验证通过", budget=Budget(5.0, 30))

    assert run.status == "completed"
    second = next(prompt for prompt in worker.prompts if "你负责的子任务 1：" in prompt)
    assert "前面子任务已经产出的结果" in second
    # 第一条子任务的产出被明文带进了第二条的交接内容。
    handoff = second.split("前面子任务已经产出的结果", 1)[1]
    assert "子任务 0" in handoff
    assert "产出 ALPHA" in handoff


@pytest.mark.asyncio
async def test_planner_and_critic_calls_share_the_worker_ledger(tmp_path: Path) -> None:
    """协调开销是协作成本的一部分（§5.3）：三段调用进同一个 ``Usage``。"""
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("一步"))
        if route == "critic":
            return ScriptedTurn(final=_critic_json(True))
        return ScriptedTurn(final="做完了")

    agent = build_l2(llm=_ScriptLLM(script), workspace=tmp_path, tools=tools)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 30))

    # planner 1 + worker 1 + critic 1 = 3 次调用，每笔都记账。
    assert run.usage.llm_calls == 3
    assert run.usage.cost_usd == pytest.approx(0.003)


@pytest.mark.asyncio
async def test_all_l2_stages_share_one_global_cost_limit(tmp_path: Path) -> None:
    """planner 与 worker 用完预算后，不再给 Critic 开一份新的满额预算。"""
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("一步"))
        if route == "critic":
            return ScriptedTurn(final=_critic_json(True))
        return ScriptedTurn(final="做完了")

    llm = _ScriptLLM(script, cost_usd=0.001)
    agent = build_l2(llm=llm, workspace=tmp_path, tools=tools)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(0.002, 30))

    assert run.status == "budget_exhausted"
    assert run.usage.llm_calls == 2
    assert run.usage.cost_usd == pytest.approx(0.002)
    assert len(llm.seen) == 2


@pytest.mark.asyncio
async def test_zero_step_budget_does_not_call_the_planner(tmp_path: Path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    llm = _ScriptLLM(lambda messages: ScriptedTurn(final=_plan_json("一步")))
    agent = build_l2(llm=llm, workspace=tmp_path, tools=tools)

    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 0))

    assert run.status == "step_limit"
    assert run.usage.llm_calls == 0
    assert llm.seen == []


@pytest.mark.asyncio
async def test_critic_rejection_reruns_the_plan_with_the_revision_instruction(
    tmp_path: Path,
) -> None:
    """L2 的打回重跑整条计划，并把打回理由带进每条重跑交接。"""
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    state = {"critiques": 0}
    worker = _Worker(["A", "B"], revised={1: "B-fixed"})

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("子任务 A", "子任务 B"))
        if route == "critic":
            state["critiques"] += 1
            if state["critiques"] == 1:
                return ScriptedTurn(
                    final=_critic_json(False, "子任务 B 漏了边界情况")
                )
            return ScriptedTurn(final=_critic_json(True))
        return worker.turn(messages)

    agent = build_l2(llm=_ScriptLLM(script), workspace=tmp_path, tools=tools)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 30))

    assert run.status == "completed"
    assert run.review_rounds == 1
    # 初次两条（各两轮）+ 全量重跑两条（各两轮）。
    assert len(worker.prompts) == 8
    # 重跑两条都带打回理由，后一条仍拿到前一条的新产出。
    rerun = [prompt for prompt in worker.prompts if "打回" in prompt]
    assert len(rerun) == 4
    assert any("你负责的子任务 0：" in prompt for prompt in rerun)
    assert any("你负责的子任务 1：" in prompt for prompt in rerun)
    assert "漏了边界情况" in rerun[0]
    revised = next(prompt for prompt in rerun if "你负责的子任务 1：" in prompt)
    assert "前面子任务已经产出的结果" in revised


@pytest.mark.asyncio
async def test_unreadable_plan_is_an_honest_error(tmp_path: Path) -> None:
    """planner 没产出可读计划时报 error，不自己编一份计划接着跑。"""
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        if _route(messages) == "planner":
            return ScriptedTurn(final="我觉得这个任务还挺复杂的，先看看吧")
        return ScriptedTurn(final=_critic_json(True))

    agent = build_l2(llm=_ScriptLLM(script), workspace=tmp_path, tools=tools)
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 30))

    assert run.status == "error"
    assert run.subtask_count == 0
    assert run.stop_reason


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


def _two_step_repo(tmp_path: Path) -> Path:
    """一个两步才能做完的合成任务：两个常量都在，验证才过。

    刻意是**两个互补的洞**：只写 A 或只写 B 都过不了验证，所以"两步都做了"是硬判据，
    而不是脚本自己声称的。零 git 历史依赖，任何机器上都能跑（不再 skip）。
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "config.py").write_text("A = 0\nB = 0\n", encoding="utf-8")
    (repo / "tests" / "test_two_step.py").write_text(
        "def test_noop():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "bench@localhost")
    _git(repo, "config", "user.name", "bench")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "baseline")
    base = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "tests" / "test_two_step.py").write_text(
        "from src import config\n\n\n"
        "def test_both_constants_are_set():\n"
        "    assert config.A == 1\n"
        "    assert config.B == 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-gpg-sign", "-m", "add verification")
    test_tip = _git(repo, "rev-parse", "HEAD").strip()

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "two-step.test.patch").write_text(
        _git(repo, "diff", base, test_tip), encoding="utf-8"
    )
    (tasks / "two-step.task.json").write_text(
        json.dumps(
            {
                "task_id": "two-step",
                "base_commit": base,
                "verify_command": [
                    "{python}",
                    "-m",
                    "pytest",
                    "tests/test_two_step.py::test_both_constants_are_set",
                    "-q",
                ],
                "test_paths": ["tests/test_two_step.py"],
                "difficulty": "易",
                "source": "E3",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return repo


@pytest.mark.asyncio
async def test_two_step_task_passes_end_to_end_through_the_harness(tmp_path: Path) -> None:
    """收工判据：一个需要两步才能做完的任务，在 L2 上端到端 `pass`。"""
    repo = _two_step_repo(tmp_path)
    spec = load_task(tmp_path / "tasks" / "two-step.task.json")
    worker = _Worker(["A = 1\nB = 0\n", "A = 1\nB = 2\n"], path="src/config.py")

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(
                final=_plan_json("把 A 设成 1（只改 A）", "把 B 设成 2（在 A 已改好的基础上）")
            )
        if route == "critic":
            return ScriptedTurn(final=_critic_json(True))
        return worker.turn(messages)

    result = await run_task(
        spec=spec,
        agent_factory=resolve_arm("L2", llm=_ScriptLLM(script)),
        repo=repo,
        workdir=tmp_path / "work",
        arm="L2",
        budget=Budget(max_cost_usd=1.0, max_steps=30),
    )

    assert result.outcome == "pass", result.verdict_output
    assert result.agent_run.status == "completed"
    assert result.agent_run.subtask_count == 2
    assert "A = 1" in result.patch and "B = 2" in result.patch
    # 第二条子任务的交接里带着第一条的产出。
    second = next(prompt for prompt in worker.prompts if "你负责的子任务 1：" in prompt)
    assert "前面子任务已经产出的结果" in second


@pytest.mark.asyncio
async def test_l2_runs_a_real_repo_task_without_crashing(tmp_path: Path) -> None:
    """L2 接仓库自带的真实任务时要能跑完并落一份自报，不崩。

    这里不 chasing ``pass``：判断"真的修好了"需要真模型；本用例钉的是拓扑能在真实
    任务上跑到底、结构字段齐全。
    """
    spec: TaskSpec = load_task(TASK_FILE)

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("修 app/middleware/auth.py"))
        if route == "critic":
            return ScriptedTurn(final=_critic_json(True))
        return ScriptedTurn(final="做完了")

    result = await run_task(
        spec=spec,
        agent_factory=resolve_arm("L2", llm=_ScriptLLM(script)),
        repo=ROOT,
        workdir=tmp_path / "work",
        arm="L2",
        budget=Budget(max_cost_usd=1.0, max_steps=30),
    )

    assert result.agent_run.status in ("completed", "step_limit", "budget_exhausted", "error")
    assert result.agent_run.subtask_count == 1
