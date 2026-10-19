from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.agents.contract import AgentEnvelope
from app.agents.provider import ChatResult


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class ScriptedToolLLM:
    """按脚本返回工具调用或终局；终局可以是 callable 以便读取工具观察。"""

    def __init__(self, turns: list) -> None:
        self._turns = list(turns)
        self.tool_schemas: list[dict] = []
        self.calls = 0
        self.chat_calls = 0

    async def chat(self, messages: list[dict], **kwargs) -> str:
        self.chat_calls += 1
        return json.dumps(
            {
                "findings": [],
                "summary": "legacy path",
                "recommendation": "adequate",
            },
            ensure_ascii=False,
        )

    async def chat_with_tools(self, messages: list[dict], tools: list[dict], **kwargs) -> ChatResult:
        self.calls += 1
        self.tool_schemas = tools
        turn = self._turns.pop(0)
        if callable(turn):
            content = turn(messages)
            return ChatResult(
                messages=[*messages, {"role": "assistant", "content": content}],
                content=content,
                tool_calls=[],
            )
        if isinstance(turn, str):
            return ChatResult(
                messages=[*messages, {"role": "assistant", "content": turn}],
                content=turn,
                tool_calls=[],
            )
        return ChatResult(
            messages=[
                *messages,
                {"role": "assistant", "content": None, "tool_calls": turn},
            ],
            content="",
            tool_calls=turn,
        )


def _envelope(tmp_path: Path) -> AgentEnvelope:
    return AgentEnvelope(
        trace_id="trace-coverage",
        session_id="session-coverage",
        user_raw_input="",
        global_summary="",
        agent_local_slot={
            "diff": "+def divide(a, b):\n+    return a / b",
            "changed_files": [{"filename": "app/calc.py"}],
            "workspace_path": str(tmp_path),
            "verify_command": [sys.executable, "-c", "print('1 passed')"],
        },
    )


def _final_with_evidence(messages: list[dict]) -> str:
    tool_outputs = [
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "tool"
    ]
    return json.dumps(
        {
            "findings": [
                {
                    "id": "test-001",
                    "severity": "low",
                    "category": "test_coverage",
                    "file": "tests/test_calc.py",
                    "line": 1,
                    "title": "补充除零测试",
                    "description": "边界条件仍需覆盖",
                    "suggestion": "添加除零断言",
                    "confidence": 0.8,
                    "evidence": tool_outputs,
                }
            ],
            "summary": "已实际运行测试",
            "recommendation": "needs_improvement",
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_coverage_agent_runs_tests_and_keeps_tool_evidence(tmp_path: Path) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    llm = ScriptedToolLLM(
        [
            [_tool_call("read-1", "read_file", {"path": "app/calc.py"})],
            [_tool_call("tests-1", "run_tests", {})],
            _final_with_evidence,
        ]
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "calc.py").write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")

    output = await TestCoverageAgent(llm=llm).execute(_envelope(tmp_path))
    payload = json.loads(output)

    assert [tool["function"]["name"] for tool in llm.tool_schemas] == [
        "read_file",
        "run_tests",
    ]
    assert llm.calls == 3
    assert payload["recommendation"] == "needs_improvement"
    assert "1 passed" in payload["findings"][0]["evidence"][-1]


@pytest.mark.asyncio
async def test_coverage_agent_downgrades_when_tests_were_not_run(tmp_path: Path) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    llm = ScriptedToolLLM(
        [
            json.dumps(
                {
                    "findings": [],
                    "summary": "看起来测试覆盖足够",
                    "recommendation": "adequate",
                },
                ensure_ascii=False,
            )
        ]
    )

    output = await TestCoverageAgent(llm=llm).execute(_envelope(tmp_path))
    payload = json.loads(output)

    assert payload["recommendation"] == "insufficient"
    assert any(
        finding["title"] == "覆盖率结论未经过实际测试"
        for finding in payload["findings"]
    )


@pytest.mark.asyncio
async def test_coverage_agent_downgrades_when_run_tests_was_rejected(tmp_path: Path) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    llm = ScriptedToolLLM(
        [
            [_tool_call("tests-1", "run_tests", {"selector": "--rootdir=/"})],
            json.dumps(
                {
                    "findings": [],
                    "summary": "测试看起来通过",
                    "recommendation": "adequate",
                },
                ensure_ascii=False,
            ),
        ]
    )

    output = await TestCoverageAgent(llm=llm).execute(_envelope(tmp_path))
    payload = json.loads(output)

    assert payload["recommendation"] == "insufficient"
    assert any(
        finding["title"] == "覆盖率结论未经过实际测试"
        for finding in payload["findings"]
    )


@pytest.mark.asyncio
async def test_coverage_agent_downgrades_on_workspace_error(tmp_path: Path) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    llm = ScriptedToolLLM(
        [
            json.dumps(
                {
                    "findings": [],
                    "summary": "看起来没问题",
                    "recommendation": "adequate",
                },
                ensure_ascii=False,
            )
        ]
    )
    envelope = _envelope(tmp_path)
    envelope.agent_local_slot["workspace_error"] = "workspace download failed"

    output = await TestCoverageAgent(llm=llm).execute(envelope)
    payload = json.loads(output)

    assert llm.chat_calls == 1
    assert llm.calls == 0
    assert payload["recommendation"] == "insufficient"
    assert "workspace download failed" in payload["summary"]


@pytest.mark.asyncio
async def test_coverage_agent_without_workspace_keeps_legacy_path(tmp_path: Path) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    llm = ScriptedToolLLM([])
    envelope = _envelope(tmp_path)
    envelope.agent_local_slot.pop("workspace_path")

    output = await TestCoverageAgent(llm=llm).execute(envelope)

    assert llm.chat_calls == 1
    assert llm.calls == 0
    assert json.loads(output)["summary"] == "legacy path"
