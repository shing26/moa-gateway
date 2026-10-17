"""L3 档：并发 worker、reducer 合并与子任务级定向重跑。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.agents.provider import ChatResult

from bench.agent.expert import ExpertPool
from bench.agent.scripted import ScriptedLLM, ScriptedTurn
from bench.agent.tools import build_toolset
from bench.agent.types import Budget
from bench.arms import arm_names, resolve_arm
from bench.arms.l3 import L3ConcurrentWorkers

PASSING_VERIFY = ("python", "-c", "pass")


def test_l3_is_registered() -> None:
    assert "L3" in arm_names()


def test_registry_builds_l3_through_the_shared_arm_interface(tmp_path: Path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    factory = resolve_arm(
        "L3",
        llm=ScriptedLLM([ScriptedTurn(final="APPROVED")]),
    )

    agent = factory(workspace=tmp_path, tools=tools)

    assert isinstance(agent, L3ConcurrentWorkers)


@pytest.mark.asyncio
async def test_registry_build_runs_with_independent_cloned_workers(tmp_path: Path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)

    def router(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("一步"))
        if route == "critic":
            return ScriptedTurn(final="APPROVED")
        return ScriptedTurn(final="完成")

    factory = resolve_arm("L3", llm=_RouterLLM(router))
    run = await factory(workspace=tmp_path, tools=tools).run(
        workspace=tmp_path,
        goal="目标",
        budget=Budget(5.0, 30),
    )

    assert run.status == "completed"
    assert run.subtask_usage[0].model == "scripted"


class _RouterLLM:
    """L3 的 planner / Critic 共用一条串行客户端；worker 各自独立建 client。"""

    def __init__(self, script: Any) -> None:
        self._script = script
        self.config = type("_Cfg", (), {"model": "scripted", "temperature": 0.0})()
        self.last_metrics: dict[str, Any] = {}

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatResult:
        self.last_metrics = {
            "model_used": self.config.model,
            "cost_usd": 0.001,
            "llm_latency_ms": 0.0,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
        turn = self._script(messages)
        if turn.tool:
            call = {
                "id": "router-call",
                "type": "function",
                "function": {
                    "name": turn.tool,
                    "arguments": json.dumps(turn.arguments, ensure_ascii=False),
                },
            }
            return ChatResult(messages=[*messages], content="", tool_calls=[call])
        return ChatResult(messages=[*messages], content=turn.final, tool_calls=[])


class _BarrierWorkerLLM:
    """第一次调用先在 barrier 会合，证明三个 worker 真在同一个 wave 里跑。"""

    def __init__(self, *, index: int, barrier: asyncio.Barrier) -> None:
        self._index = index
        self._barrier = barrier
        self._pending = False
        self.config = type("_Cfg", (), {"model": f"worker-{index}", "temperature": 0.0})()
        self.last_metrics: dict[str, Any] = {}

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatResult:
        self.last_metrics = {
            "model_used": self.config.model,
            "cost_usd": 0.01 + self._index / 100,
            "llm_latency_ms": 0.0,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
        if not self._pending:
            await self._barrier.wait()
            self._pending = True
            call = {
                "id": f"worker-{self._index}",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {"path": f"worker-{self._index}.txt", "content": f"W{self._index}"},
                        ensure_ascii=False,
                    ),
                },
            }
            return ChatResult(messages=[*messages], content="", tool_calls=[call])
        return ChatResult(messages=[*messages], content=f"worker {self._index} 完成", tool_calls=[])


class _RevisionWorkerLLM:
    """每个 factory lane 的两轮脚本；第二次创建 lane 时写入修好的内容。"""

    def __init__(self, *, index: int, lane: int) -> None:
        self._index = index
        self._lane = lane
        self._pending = False
        self.config = type("_Cfg", (), {"model": f"worker-{index}", "temperature": 0.0})()
        self.last_metrics: dict[str, Any] = {}

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatResult:
        self.last_metrics = {
            "model_used": self.config.model,
            "cost_usd": 0.01 + self._index / 100,
            "llm_latency_ms": 0.0,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
        if not self._pending:
            self._pending = True
            content = "FIXED" if self._index == 1 and self._lane > 1 else f"v{self._lane}-{self._index}"
            call = {
                "id": f"revision-{self._index}-{self._lane}",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {"path": f"worker-{self._index}.txt", "content": content},
                        ensure_ascii=False,
                    ),
                },
            }
            return ChatResult(messages=[*messages], content="", tool_calls=[call])
        return ChatResult(messages=[*messages], content="完成", tool_calls=[])


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
    if "审查专家" in _system_text(messages):
        return "critic"
    if "只回一个 JSON 数组" in _user_text(messages):
        return "planner"
    return "worker"


def _plan_json(*instructions: str) -> str:
    subtasks = [
        {"instruction": instruction, "context": f"关于「{instruction}」的背景事实"}
        for instruction in instructions
    ]
    return "```json\n" + json.dumps({"subtasks": subtasks}, ensure_ascii=False) + "\n```"


def _revise_json(instructions: str, targets: tuple[int, ...]) -> str:
    return json.dumps(
        {
            "verdict": "revise",
            "instructions": instructions,
            "targets": list(targets),
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_three_workers_run_in_one_concurrent_wave(tmp_path: Path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    barrier = asyncio.Barrier(3)

    def router(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("写 worker-0", "写 worker-1", "写 worker-2"))
        if route == "critic":
            return ScriptedTurn(final="APPROVED")
        raise AssertionError("worker 不该走共享 router client")

    agent = L3ConcurrentWorkers(
        pool=ExpertPool(llm=_RouterLLM(router), tools=tools),
        workspace=tmp_path,
        worker_llm_factory=lambda index: _BarrierWorkerLLM(index=index, barrier=barrier),
    )

    run = await asyncio.wait_for(
        agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 30)),
        timeout=5.0,
    )

    assert run.status == "completed"
    assert run.subtask_count == 3
    assert {path.name for path in tmp_path.glob("worker-*.txt")} == {
        "worker-0.txt",
        "worker-1.txt",
        "worker-2.txt",
    }
    assert not barrier.broken
    worker_usage = {item.index: item for item in run.subtask_usage}
    assert set(worker_usage) == {0, 1, 2}
    assert [worker_usage[index].usage.cost_usd for index in (0, 1, 2)] == pytest.approx(
        [0.02, 0.04, 0.06]
    )
    assert [worker_usage[index].model for index in (0, 1, 2)] == [
        "worker-0",
        "worker-1",
        "worker-2",
    ]
    assert run.usage.cost_usd == pytest.approx(0.122)


@pytest.mark.asyncio
async def test_targeted_revision_reruns_only_the_named_subtask(tmp_path: Path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    critiques = {"count": 0}
    lanes: dict[int, int] = {0: 0, 1: 0, 2: 0}

    def router(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("写 0", "写 1", "写 2"))
        if route == "critic":
            critiques["count"] += 1
            if critiques["count"] == 1:
                return ScriptedTurn(final=_revise_json("只修子任务 1", (1,)))
            return ScriptedTurn(final="APPROVED")
        raise AssertionError("worker 不该走共享 router client")

    def worker_factory(index: int) -> _RevisionWorkerLLM:
        lanes[index] += 1
        return _RevisionWorkerLLM(index=index, lane=lanes[index])

    agent = L3ConcurrentWorkers(
        pool=ExpertPool(llm=_RouterLLM(router), tools=tools),
        workspace=tmp_path,
        worker_llm_factory=worker_factory,
    )
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 30))

    assert run.status == "completed"
    assert run.review_rounds == 1
    assert lanes == {0: 1, 1: 2, 2: 1}
    assert (tmp_path / "worker-1.txt").read_text(encoding="utf-8").strip() == "FIXED"
    assert {item.index for item in run.subtask_usage} == {0, 1, 2}
    # 账本可归因：只重跑的 1 号两轮用量累加，没重跑的下标保持单轮。
    worker_usage = {item.index: item for item in run.subtask_usage}
    assert worker_usage[0].usage.cost_usd == pytest.approx(0.02)
    assert worker_usage[2].usage.cost_usd == pytest.approx(0.06)
    assert worker_usage[1].usage.cost_usd == pytest.approx(0.08)
    # 每轮一次工具调用（写文件）后交终局：单轮 turns=1，两轮累加成 2。
    assert worker_usage[1].turns == 2


@pytest.mark.asyncio
async def test_concurrent_over_budget_stops_before_the_critic(tmp_path: Path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)
    critic_calls = {"count": 0}

    def router(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("worker 0", "worker 1", "worker 2"))
        if route == "critic":
            critic_calls["count"] += 1
            return ScriptedTurn(final="APPROVED")
        raise AssertionError("worker 不该走共享 router client")

    agent = L3ConcurrentWorkers(
        pool=ExpertPool(llm=_RouterLLM(router), tools=tools),
        workspace=tmp_path,
        worker_llm_factory=lambda index: ScriptedLLM(
            [
                ScriptedTurn(
                    tool="list_files",
                    arguments={},
                    cost_usd=0.02,
                )
            ],
            model=f"worker-{index}",
        ),
    )
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(0.01, 30))

    assert run.status == "budget_exhausted"
    assert critic_calls["count"] == 0
    assert {item.status for item in run.subtask_usage} == {"budget_exhausted"}
    assert run.usage.cost_usd > 0.01


@pytest.mark.asyncio
async def test_one_worker_factory_failure_is_an_error_not_a_crash(tmp_path: Path) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)

    def router(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("worker 0", "worker 1", "worker 2"))
        if route == "critic":
            return ScriptedTurn(final="APPROVED")
        raise AssertionError("worker 不该走共享 router client")

    def factory(index: int) -> ScriptedLLM:
        if index == 1:
            raise RuntimeError("client 创建失败")
        return ScriptedLLM([ScriptedTurn(final="完成")], model=f"worker-{index}")

    agent = L3ConcurrentWorkers(
        pool=ExpertPool(llm=_RouterLLM(router), tools=tools),
        workspace=tmp_path,
        worker_llm_factory=factory,
    )
    run = await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 30))

    assert run.status == "error"
    failed = next(item for item in run.subtask_usage if item.index == 1)
    assert failed.status == "error"
    assert "client 创建失败" in run.error
    # 不造假的工具步：错误经 AgentRun.error 上浮，轨迹里不留伪证据。
    assert run.steps == ()


class _CancellingLLM:
    """一个 worker lane 的 LLM 抛 ``CancelledError``——这条不是"某个 worker 出错"。"""

    def __init__(self) -> None:
        self.config = type("_Cfg", (), {"model": "cancel", "temperature": 0.0})()
        self.last_metrics: dict[str, Any] = {}

    async def chat_with_tools(self, messages, tools, **kwargs: Any) -> ChatResult:
        raise asyncio.CancelledError("lane cancelled")


@pytest.mark.asyncio
async def test_worker_cancellation_propagates_instead_of_becoming_error(
    tmp_path: Path,
) -> None:
    tools = build_toolset(workspace=tmp_path, verify_command=PASSING_VERIFY)

    def router(messages: list[dict[str, Any]]) -> ScriptedTurn:
        route = _route(messages)
        if route == "planner":
            return ScriptedTurn(final=_plan_json("worker 0", "worker 1"))
        if route == "critic":
            return ScriptedTurn(final="APPROVED")
        raise AssertionError("worker 不该走共享 router client")

    agent = L3ConcurrentWorkers(
        pool=ExpertPool(llm=_RouterLLM(router), tools=tools),
        workspace=tmp_path,
        worker_llm_factory=lambda index: _CancellingLLM(),
    )

    # ADR-010 决策 4：取消原样上抛，绝不被收成 status="error" 的一条可审计失败。
    with pytest.raises(asyncio.CancelledError):
        await agent.run(workspace=tmp_path, goal="目标", budget=Budget(5.0, 30))
