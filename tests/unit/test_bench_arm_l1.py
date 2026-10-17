"""L1 档：单 agent + Critic 有界回边（§5 的四档表）。

两条路径各一条离线用例：
* Critic 打回一次 → 再修 → 通过；
* Critic 一直打回 → 在有界的轮数上停下，且**不伪装成功**。
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
from bench.arms.l1 import L1CriticAgent, build_l1
from bench.harness.runner import load_task, run_task

ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = ROOT / "bench" / "tasks" / "988fccb.task.json"
FIX_COMMIT = "988fccbb242be0573f4e42f05118276884272bf3"
FIXED_FILE = "app/middleware/auth.py"
PASSING_VERIFY = (sys.executable, "-c", "pass")


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


class _RouterLLM:
    """按 messages 分派回包：同一份脚本既跑 worker 又跑 Critic。

    真机上这两段是同一模型的两条提示；离线里靠 system prompt 里的角色分派，这样
    Critic 走的是真实的 ``review`` 专家循环（角色指令 + 工具循环），而不是测试
    自己捏的审批函数。每一轮都记下 messages，用来断言"批评真进了下一轮上下文"。
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


def _is_critic(messages: list[dict[str, Any]]) -> bool:
    return any(
        message.get("role") == "system" and "审查专家" in str(message.get("content", ""))
        for message in messages
    )


def _user_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "user"
    )


def _critic_reply(approved: bool, instructions: str = "") -> str:
    payload = {"verdict": "approve" if approved else "revise", "instructions": instructions}
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def test_l1_is_registered() -> None:
    assert "L1" in arm_names()


def test_the_critic_is_the_review_expert_from_the_pool(tmp_path: Path) -> None:
    """资源取舍：Critic 用池里的 ``review`` 专家（带工具），不另造一套评审。"""
    tools: ToolRegistry = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    agent = build_l1(llm=_RouterLLM(lambda m: ScriptedTurn(final="")), workspace=tmp_path, tools=tools)

    assert isinstance(agent, L1CriticAgent)
    assert isinstance(agent._critic, ToolExpert)
    assert agent._critic.key == "review"


@pytest.mark.asyncio
async def test_critic_and_worker_share_one_ledger(tmp_path: Path) -> None:
    """Critic 的调用与 worker 进同一份 ``Usage``；协调开销不许漏记（§5.3）。"""
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    llm = _RouterLLM(
        lambda messages: ScriptedTurn(final=_critic_reply(True))
        if _is_critic(messages)
        else ScriptedTurn(final="做完了")
    )
    agent = L1CriticAgent(pool=ExpertPool(llm=llm, tools=tools), workspace=tmp_path)

    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 20))

    assert run.status == "completed"
    assert run.review_rounds == 0
    # worker 1 次 + critic 1 次，两份用量在同一个 llm_calls 上。
    assert run.usage.llm_calls == 2
    assert run.usage.cost_usd == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_revise_instruction_enters_the_next_context(tmp_path: Path) -> None:
    """Critic 的反馈必须落到下一轮的上下文里，而不是只写进日志。"""
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    instruction = "把 app/middleware/auth.py 的 return True 改成 fail-closed"
    revised = {"done": False}

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        if _is_critic(messages):
            if revised["done"]:
                return ScriptedTurn(final=_critic_reply(True))
            return ScriptedTurn(final=_critic_reply(False, instruction))
        # 第一轮先自报完成，等 Critic 打回后再按指令改。
        if "修正指令" in _user_text(messages):
            revised["done"] = True
            return ScriptedTurn(final="改好了")
        return ScriptedTurn(final="做完了")

    llm = _RouterLLM(script)
    agent = L1CriticAgent(pool=ExpertPool(llm=llm, tools=tools), workspace=tmp_path)

    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 20))

    assert run.status == "completed"
    assert run.review_rounds == 1
    revised = [call for call in llm.seen if "修正指令" in _user_text(call)]
    assert revised, "修正指令没有进 worker 的下一轮上下文"
    assert instruction in _user_text(revised[0])


@pytest.mark.asyncio
@requires_history
async def test_revise_then_fix_passes_end_to_end(tmp_path: Path) -> None:
    """打回一次 → 按指令再修 → 判决 ``pass``。"""
    fixed = _show(f"{FIX_COMMIT}:{FIXED_FILE}")
    state = {"critic_saw_revision": False, "attempt": "initial", "pending_finish": False}

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        if _is_critic(messages):
            # worker 的修订发生在"Critic 说打回之后"——所以第一条打回时它还没改。
            if state["attempt"] != "initial":
                state["critic_saw_revision"] = True
                return ScriptedTurn(final=_critic_reply(True))
            return ScriptedTurn(
                final=_critic_reply(
                    False,
                    "把 app/middleware/auth.py 换成带 _DASH_PROTECTED_PREFIXES 的版本",
                )
            )
        if state["pending_finish"]:
            state["pending_finish"] = False
            return ScriptedTurn(final="做完了")
        if "修正指令" in _user_text(messages):
            state["attempt"] = "revised"
            state["pending_finish"] = True
            return ScriptedTurn(
                tool="write_file",
                arguments={"path": FIXED_FILE, "content": fixed},
            )
        state["pending_finish"] = True
        return ScriptedTurn(
            tool="write_file",
            arguments={"path": FIXED_FILE, "content": "# 还没修好\n"},
        )

    result = await run_task(
        spec=load_task(TASK_FILE),
        agent_factory=resolve_arm("L1", llm=_RouterLLM(script)),
        repo=ROOT,
        workdir=tmp_path / "work",
        arm="L1",
        budget=Budget(max_cost_usd=1.0, max_steps=30),
    )

    assert result.outcome == "pass", result.verdict_output
    assert result.agent_run.status == "completed"
    assert result.agent_run.review_rounds == 1
    assert result.patch_touched_tests is False
    assert "_DASH_PROTECTED_PREFIXES" in result.patch
    assert state["critic_saw_revision"] is True


@pytest.mark.asyncio
@requires_history
async def test_critic_never_approves_stops_at_the_round_cap(tmp_path: Path) -> None:
    """一直打回：到硬上限就停，报 ``step_limit``，绝不伪装成功。"""
    attempts = {"n": 0}
    wrote = {"pending": False}

    def script(messages: list[dict[str, Any]]) -> ScriptedTurn:
        if _is_critic(messages):
            return ScriptedTurn(final=_critic_reply(False, "还是不对，再改"))
        if wrote["pending"]:
            wrote["pending"] = False
            return ScriptedTurn(final=f"第 {attempts['n']} 次做完了")
        attempts["n"] += 1
        wrote["pending"] = True
        return ScriptedTurn(
            tool="write_file",
            arguments={"path": FIXED_FILE, "content": f"# 尝试 {attempts['n']}\n"},
        )

    llm = _RouterLLM(script)
    result = await run_task(
        spec=load_task(TASK_FILE),
        agent_factory=resolve_arm("L1", llm=llm),
        repo=ROOT,
        workdir=tmp_path / "work",
        arm="L1",
        budget=Budget(max_cost_usd=1.0, max_steps=30),
    )

    assert result.outcome == "fail"
    assert result.agent_run.status == "step_limit"
    # 默认硬上限 2 轮：worker 一共尝试了 1 + 2 次。
    assert result.agent_run.review_rounds == 2
    assert attempts["n"] == 3
