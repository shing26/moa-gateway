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

    def __init__(self, turns: list, *, chat_turns: list[str] | None = None) -> None:
        self._turns = list(turns)
        self._chat_turns = list(chat_turns or [])
        self.tool_schemas: list[dict] = []
        self.calls = 0
        self.chat_calls = 0

    async def chat(self, messages: list[dict], **kwargs) -> str:
        self.chat_calls += 1
        if self._chat_turns:
            return self._chat_turns.pop(0)
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
async def test_coverage_agent_keeps_test_output_when_model_final_is_empty(
    tmp_path: Path,
) -> None:
    """A final empty tool response must not erase a successful command run."""
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    llm = ScriptedToolLLM(
        [
            [_tool_call("tests-1", "run_tests", {})],
            "",
        ]
    )

    output = await TestCoverageAgent(llm=llm).execute(_envelope(tmp_path))
    payload = json.loads(output)

    assert payload["recommendation"] == "insufficient"
    assert payload["need_human_review"] is True
    assert payload["findings"][0]["title"] == "验证命令已执行"
    assert "1 passed" in payload["findings"][0]["evidence"][0]


@pytest.mark.asyncio
async def test_coverage_agent_ignores_duplicate_run_tests_requests(
    tmp_path: Path,
) -> None:
    """同一次 agent 执行只能真实运行一次验证命令。"""
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    marker = tmp_path / "runs.txt"
    envelope = _envelope(tmp_path)
    envelope.agent_local_slot["verify_command"] = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"p = Path({str(marker)!r}); "
            "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1', "
            "encoding='utf-8'); print('1 passed')"
        ),
    ]
    llm = ScriptedToolLLM(
        [
            [
                _tool_call("tests-1", "run_tests", {}),
                _tool_call("tests-2", "run_tests", {}),
            ],
            _final_with_evidence,
        ]
    )

    output = await TestCoverageAgent(llm=llm).execute(envelope)
    payload = json.loads(output)

    assert marker.read_text(encoding="utf-8") == "1"
    assert llm.calls == 2
    assert payload["recommendation"] == "needs_improvement"


@pytest.mark.asyncio
async def test_coverage_agent_runs_fixed_command_when_model_skips_tools(
    tmp_path: Path,
) -> None:
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
    assert payload["need_human_review"] is True
    assert payload["summary"].startswith("验证命令由运行器补执行")
    assert payload["findings"][0]["title"] == "验证命令已执行"
    assert "1 passed" in payload["findings"][0]["evidence"][0]


@pytest.mark.asyncio
async def test_coverage_agent_cannot_keep_adequate_after_failed_command(
    tmp_path: Path,
) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    envelope = _envelope(tmp_path)
    envelope.agent_local_slot["verify_command"] = [
        sys.executable,
        "-c",
        "raise SystemExit(1)",
    ]
    llm = ScriptedToolLLM(
        [
            json.dumps(
                {
                    "findings": [],
                    "summary": "模型先给出了错误结论",
                    "recommendation": "adequate",
                },
                ensure_ascii=False,
            )
        ]
    )

    output = await TestCoverageAgent(llm=llm).execute(envelope)
    payload = json.loads(output)

    assert payload["recommendation"] == "insufficient"
    assert payload["need_human_review"] is True
    assert payload["summary"].startswith("验证命令未通过。")
    assert "退出码 1（未通过）" in payload["findings"][0]["evidence"][0]


@pytest.mark.asyncio
async def test_coverage_agent_keeps_test_evidence_only_once(tmp_path: Path) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    def final_with_duplicate_evidence(messages: list[dict]) -> str:
        test_output = next(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "tool"
            and "退出码" in str(message.get("content", ""))
        )
        finding = {
            "id": "test-001",
            "severity": "medium",
            "category": "test_coverage",
            "file": "app/calc.py",
            "line": 1,
            "title": "补充分支测试",
            "description": "边界条件仍需覆盖",
            "suggestion": "添加断言",
            "confidence": 0.8,
            "evidence": [test_output],
        }
        return json.dumps(
            {
                "findings": [
                    finding,
                    {
                        **finding,
                        "id": "test-002",
                        "line": 2,
                        "evidence": [test_output],
                    },
                ],
                "summary": "已实际运行测试",
                "recommendation": "needs_improvement",
            },
            ensure_ascii=False,
        )

    llm = ScriptedToolLLM(
        [
            [_tool_call("tests-1", "run_tests", {})],
            final_with_duplicate_evidence,
        ]
    )

    output = await TestCoverageAgent(llm=llm).execute(_envelope(tmp_path))
    payload = json.loads(output)

    assert sum(
        evidence_item.count("退出码 0") == 1
        for finding in payload["findings"]
        for evidence_item in finding["evidence"]
    ) == 1
    assert payload["findings"][1]["evidence"] == []


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
async def test_coverage_agent_downgrades_rejected_tool_with_bad_json(
    tmp_path: Path,
) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    llm = ScriptedToolLLM(
        [
            [_tool_call("tests-1", "run_tests", {"selector": "--rootdir=/"})],
            "not-json",
        ]
    )

    output = await TestCoverageAgent(llm=llm).execute(_envelope(tmp_path))
    payload = json.loads(output)

    assert payload["recommendation"] == "insufficient"
    assert payload["need_human_review"] is True
    assert "不在允许列表内" in payload["summary"]
    assert payload["findings"][0]["title"] == "覆盖率结论未经过实际测试"


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
async def test_coverage_agent_downgrades_empty_response_on_workspace_error(
    tmp_path: Path,
) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    llm = ScriptedToolLLM([], chat_turns=[""])
    envelope = _envelope(tmp_path)
    envelope.agent_local_slot["workspace_error"] = "workspace download failed"

    output = await TestCoverageAgent(llm=llm).execute(envelope)
    payload = json.loads(output)

    assert payload["recommendation"] == "insufficient"
    assert payload["need_human_review"] is True
    assert "workspace download failed" in payload["summary"]
    assert payload["findings"][0]["title"] == "覆盖率结论未经过实际测试"


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


@pytest.mark.asyncio
async def test_coverage_agent_downgrades_when_llm_cannot_call_tools(
    tmp_path: Path,
) -> None:
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    class ChatOnlyLLM:
        async def chat(self, messages: list[dict], **kwargs) -> str:
            return json.dumps(
                {
                    "findings": [],
                    "summary": "没有运行测试也给出结论",
                    "recommendation": "adequate",
                },
                ensure_ascii=False,
            )

    output = await TestCoverageAgent(llm=ChatOnlyLLM()).execute(_envelope(tmp_path))
    payload = json.loads(output)

    assert payload["recommendation"] == "insufficient"
    assert payload["need_human_review"] is True
    assert "不支持工具调用" in payload["summary"]
