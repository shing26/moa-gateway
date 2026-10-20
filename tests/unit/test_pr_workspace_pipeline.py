from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.agents.provider import ChatResult
from app.models.events import MoAEvent
from apps.code_review_pipeline.agents.code_review_pipeline import CodeReviewPipeline


@dataclass
class FakeWorkspace:
    path: Path
    verify_command: tuple[str, ...]
    error: str = ""


class FakeProvisioner:
    def __init__(self, workspace: FakeWorkspace) -> None:
        self.workspace = workspace
        self.prepared_for: list[object] = []
        self.cleaned: list[FakeWorkspace] = []

    async def prepare(self, pr: object) -> FakeWorkspace:
        self.prepared_for.append(pr)
        return self.workspace

    async def cleanup(self, workspace: FakeWorkspace) -> None:
        self.cleaned.append(workspace)


class FakeGitHub:
    def __init__(self, archive_source: Path | None = None) -> None:
        self.archive_source = archive_source
        self.archive_destinations: list[Path] = []

    async def get_pr_files(self, repo: object, pr_number: int) -> list[dict]:
        return [
            {
                "filename": "app/calc.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "+def divide(a, b):\n+    return a / b",
            }
        ]

    async def download_archive(self, repo: object, ref: str, dest: Path) -> None:
        self.archive_destinations.append(dest)
        if self.archive_source is None:
            raise RuntimeError("no archive source")
        shutil.copytree(self.archive_source, dest, dirs_exist_ok=True)


class RecordingAgent:
    def __init__(self, response: str = '{"findings":[],"summary":"ok","recommendation":"approve"}') -> None:
        self.response = response
        self.envelopes = []

    async def execute(self, envelope):
        self.envelopes.append(envelope)
        return self.response


class ReportRecordingAgent:
    def __init__(self) -> None:
        self.envelopes = []

    async def execute(self, envelope):
        from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult

        self.envelopes.append(envelope)
        return AgentFindingResult(
            agent="report",
            trace_id=envelope.trace_id,
            findings=(),
            summary="ok",
            recommendation="approve",
        )


class ToolCallingCoverageLLM:
    def __init__(self, turns: list) -> None:
        self._turns = list(turns)
        self.tool_names: list[str] = []
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResult:
        self.calls += 1
        self.tool_names = [
            str((tool.get("function") or {}).get("name", "")) for tool in tools
        ]
        turn = self._turns.pop(0)
        if callable(turn):
            content = turn(messages)
            return ChatResult(
                messages=[*messages, {"role": "assistant", "content": content}],
                content=content,
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


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _coverage_final(messages: list[dict]) -> str:
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
                    "file": "app/calc.py",
                    "line": 1,
                    "title": "补充分支测试",
                    "description": "已实际运行测试",
                    "suggestion": "补充边界断言",
                    "confidence": 0.9,
                    "evidence": tool_outputs,
                }
            ],
            "summary": "测试已实际运行",
            "recommendation": "needs_improvement",
        },
        ensure_ascii=False,
    )


def _event(text: str = "") -> MoAEvent:
    return MoAEvent(
        trace_id="cr_o/r#7@sha-abc",
        event=None,
        session_id="o/r",
        text=text,
        context={
            "action": "synchronize",
            "repository": {"full_name": "o/r"},
            "pull_request": {
                "number": 7,
                "title": "run tests",
                "head": {"sha": "sha-abc"},
                "base": {"sha": "sha-base"},
                "user": {"login": "dev"},
            },
        },
    )


@pytest.mark.asyncio
async def test_pipeline_passes_workspace_and_cleans_it(tmp_path: Path) -> None:
    workspace = FakeWorkspace(
        path=tmp_path / "pr-workspace",
        verify_command=("python", "-m", "pytest", "-q"),
    )
    provisioner = FakeProvisioner(workspace)
    pipeline = CodeReviewPipeline(
        github_client=FakeGitHub(),  # type: ignore[arg-type]
        workspace_provisioner=provisioner,  # type: ignore[arg-type]
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    coverage = RecordingAgent(
        json.dumps(
            {
                "findings": [],
                "summary": "ran tests",
                "recommendation": "adequate",
            }
        )
    )
    pipeline._test = coverage  # noqa: SLF001
    pipeline._report = ReportRecordingAgent()  # noqa: SLF001

    pr, result = await pipeline.run(_event("please review"))

    assert pr.head_sha == "sha-abc"
    assert len(provisioner.prepared_for) == 1
    assert provisioner.cleaned == [workspace]
    slot = coverage.envelopes[0].agent_local_slot
    assert slot["workspace_path"] == str(workspace.path)
    assert slot["verify_command"] == ["python", "-m", "pytest", "-q"]
    assert '"diff"' in coverage.envelopes[0].user_raw_input
    assert "+def divide" in coverage.envelopes[0].user_raw_input
    assert "please review" in coverage.envelopes[0].user_raw_input
    assert result.test_coverage.recommendation == "adequate"


@pytest.mark.asyncio
async def test_pipeline_passes_workspace_error_to_coverage_agent(tmp_path: Path) -> None:
    workspace = FakeWorkspace(
        path=tmp_path,
        verify_command=(),
        error="workspace download failed",
    )
    provisioner = FakeProvisioner(workspace)
    pipeline = CodeReviewPipeline(
        github_client=FakeGitHub(),  # type: ignore[arg-type]
        workspace_provisioner=provisioner,  # type: ignore[arg-type]
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    coverage = RecordingAgent(
        '{"findings":[],"summary":"guessed","recommendation":"adequate"}'
    )
    pipeline._test = coverage  # noqa: SLF001
    pipeline._report = ReportRecordingAgent()  # noqa: SLF001

    await pipeline.run(_event())

    slot = coverage.envelopes[0].agent_local_slot
    assert slot["workspace_error"] == "workspace download failed"


@pytest.mark.asyncio
async def test_pipeline_runs_real_coverage_tools_in_pr_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.code_review_pipeline.workspace import GitHubArchiveWorkspaceProvisioner
    from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent

    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "app" / "calc.py").write_text(
        "def divide(a, b):\n    return a / b\n",
        encoding="utf-8",
    )
    client = FakeGitHub(source)
    provisioner = GitHubArchiveWorkspaceProvisioner(
        client,
        root=tmp_path / "workspaces",
        verify_command=(sys.executable, "-c", "print('workspace tests passed')"),
    )
    pipeline = CodeReviewPipeline(
        github_client=client,  # type: ignore[arg-type]
        workspace_provisioner=provisioner,
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    coverage_llm = ToolCallingCoverageLLM(
        [
            [_tool_call("read-1", "read_file", {"path": "app/calc.py"})],
            [_tool_call("tests-1", "run_tests", {})],
            _coverage_final,
        ]
    )
    pipeline._test = TestCoverageAgent(llm=coverage_llm)  # type: ignore[arg-type]  # noqa: SLF001
    pipeline._report = ReportRecordingAgent()  # noqa: SLF001

    _, result = await pipeline.run(_event())

    assert coverage_llm.tool_names == ["read_file", "run_tests"]
    assert coverage_llm.calls == 3
    assert result.test_coverage.recommendation == "needs_improvement"
    evidence = result.test_coverage.findings[0].evidence
    assert any("workspace tests passed" in item for item in evidence)
    assert client.archive_destinations
    assert not client.archive_destinations[0].exists()
