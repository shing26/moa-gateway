from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.agents.provider import ChatResult
from app.models.events import MoAEvent
from apps.code_review_pipeline.agents.code_review_pipeline import (
    CodeReviewPipeline,
    _collect_trusted_evidence,
    _has_verifiable_evidence,
)


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
                "patch": "@@ -0,0 +1,2 @@\n+def divide(a, b):\n+    return a / b",
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


class OneFindingReportAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, envelope):
        from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding

        self.calls += 1
        finding = Finding(
            id="final-001",
            severity="high",
            category="logic",
            file="app/calc.py",
            line=1,
            title="Division by zero",
            description="The new division path does not handle zero.",
            suggestion="Reject a zero divisor.",
            confidence=0.8,
            evidence=("diff:app/calc.py:1",),
            source_agents=("semantic_review", "report"),
        )
        return AgentFindingResult(
            agent="report",
            trace_id=envelope.trace_id,
            findings=(finding,),
            summary="one issue",
            recommendation="request_changes",
            need_human_review=True,
        )


class UnverifiableEvidenceReportAgent(OneFindingReportAgent):
    async def execute(self, envelope):
        from dataclasses import replace

        result = await super().execute(envelope)
        return replace(
            result,
            findings=(
                replace(
                    result.findings[0],
                    evidence=("the model says this is probably a bug",),
                ),
            ),
        )


class DropCritic:
    async def review(self, **kwargs):
        from apps.code_review_pipeline.agents.review_critic import (
            ReviewCriticResult,
            ReviewCriticVerdict,
        )

        finding = kwargs["findings"][0]
        return ReviewCriticResult(
            verdicts=(
                ReviewCriticVerdict(
                    finding_uid=finding.finding_uid,
                    decision="drop",
                    reason="The changed function is not reachable with a zero divisor.",
                ),
            ),
            metrics={"cost_usd": 0.001},
        )


class KeepCritic:
    async def review(self, **kwargs):
        from apps.code_review_pipeline.agents.review_critic import (
            ReviewCriticResult,
            ReviewCriticVerdict,
        )

        finding = kwargs["findings"][0]
        return ReviewCriticResult(
            verdicts=(
                ReviewCriticVerdict(
                    finding_uid=finding.finding_uid,
                    decision="keep",
                    reason="The model considers the issue plausible.",
                ),
            )
        )


class ReviseCritic:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, **kwargs):
        from apps.code_review_pipeline.agents.review_critic import (
            ReviewCriticResult,
            ReviewCriticVerdict,
        )

        self.calls += 1
        finding = kwargs["findings"][0]
        return ReviewCriticResult(
            verdicts=(
                ReviewCriticVerdict(
                    finding_uid=finding.finding_uid,
                    decision="revise",
                    reason="The impact is overstated.",
                    instructions="Keep the issue but narrow the description and evidence.",
                ),
            )
        )


class RevisionReportAgent(OneFindingReportAgent):
    def __init__(self) -> None:
        super().__init__()
        self.revision_instructions = None

    async def execute(self, envelope):
        if self.calls == 0:
            return await super().execute(envelope)
        from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding

        self.calls += 1
        self.revision_instructions = envelope.agent_local_slot.get("critic_instructions")
        source_uid = envelope.agent_local_slot["candidate_findings"][0]["finding_uid"]
        return AgentFindingResult(
            agent="report",
            trace_id=envelope.trace_id,
            findings=(
                Finding(
                    id="final-001-revised",
                    severity="medium",
                    category="logic",
                    file="app/calc.py",
                    line=1,
                    title="Division by zero is possible",
                    description="A zero divisor is not handled.",
                    suggestion="Reject a zero divisor and add a boundary test.",
                    confidence=0.85,
                    evidence=("diff:app/calc.py:1",),
                    source_agents=("report",),
                    revision_of=source_uid,
                ),
            ),
            summary="one narrowed issue",
            recommendation="request_changes",
            need_human_review=True,
        )


class ImplicitRevisionReportAgent(RevisionReportAgent):
    async def execute(self, envelope):
        result = await super().execute(envelope)
        if not result.findings:
            return result
        from dataclasses import replace

        return replace(
            result,
            findings=(replace(result.findings[0], revision_of=None),),
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
async def test_pipeline_excludes_critic_dropped_findings_from_final_report(
    tmp_path: Path,
) -> None:
    workspace = FakeWorkspace(path=tmp_path, verify_command=("python", "-m", "pytest", "-q"))
    pipeline = CodeReviewPipeline(
        github_client=FakeGitHub(),  # type: ignore[arg-type]
        workspace_provisioner=FakeProvisioner(workspace),  # type: ignore[arg-type]
        review_critic=DropCritic(),  # type: ignore[arg-type]
        critic_enabled=True,
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    pipeline._test = RecordingAgent()  # noqa: SLF001
    pipeline._report = OneFindingReportAgent()  # noqa: SLF001

    _, result = await pipeline.run(_event())

    assert result.report.findings == ()
    assert [item.status for item in result.persisted_findings] == ["candidate", "dropped"]
    assert result.critic_metrics["cost_usd"] == 0.001
    assert "not reachable" in result.persisted_findings[-1].critic_reason


@pytest.mark.asyncio
async def test_evidence_gate_drop_does_not_masquerade_as_critic_drop(
    tmp_path: Path,
) -> None:
    pipeline = CodeReviewPipeline(
        github_client=FakeGitHub(),  # type: ignore[arg-type]
        workspace_provisioner=FakeProvisioner(  # type: ignore[arg-type]
            FakeWorkspace(path=tmp_path, verify_command=())
        ),
        review_critic=KeepCritic(),  # type: ignore[arg-type]
        critic_enabled=True,
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    pipeline._test = RecordingAgent()  # noqa: SLF001
    pipeline._report = UnverifiableEvidenceReportAgent()  # noqa: SLF001

    _, result = await pipeline.run(_event())

    assert result.report.findings == ()
    assert [item.status for item in result.persisted_findings] == ["candidate", "dropped"]
    dropped = result.persisted_findings[-1]
    assert dropped.critic_decision == "keep"
    assert dropped.critic_reason == "evidence_gate: insufficient_evidence"


@pytest.mark.asyncio
async def test_pipeline_revises_once_and_persists_lineage(tmp_path: Path) -> None:
    workspace = FakeWorkspace(path=tmp_path, verify_command=("python", "-m", "pytest", "-q"))
    critic = ReviseCritic()
    report = RevisionReportAgent()
    pipeline = CodeReviewPipeline(
        github_client=FakeGitHub(),  # type: ignore[arg-type]
        workspace_provisioner=FakeProvisioner(workspace),  # type: ignore[arg-type]
        review_critic=critic,  # type: ignore[arg-type]
        critic_enabled=True,
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    pipeline._test = RecordingAgent()  # noqa: SLF001
    pipeline._report = report  # noqa: SLF001

    _, result = await pipeline.run(_event())

    assert critic.calls == 1
    assert report.calls == 2
    assert report.revision_instructions
    assert report.revision_instructions[0]["instructions"].startswith("Keep the issue")
    assert len(result.report.findings) == 1
    assert result.report.findings[0].severity == "medium"
    statuses = [item.status for item in result.persisted_findings]
    assert statuses == ["candidate", "superseded", "candidate", "published"]
    assert result.persisted_findings[3].revision_of == result.persisted_findings[0].finding_uid
    assert result.persisted_findings[2].finding_uid == result.persisted_findings[3].finding_uid


@pytest.mark.asyncio
async def test_pipeline_infers_revision_lineage_without_model_field(
    tmp_path: Path,
) -> None:
    workspace = FakeWorkspace(path=tmp_path, verify_command=("python", "-m", "pytest", "-q"))
    pipeline = CodeReviewPipeline(
        github_client=FakeGitHub(),  # type: ignore[arg-type]
        workspace_provisioner=FakeProvisioner(workspace),  # type: ignore[arg-type]
        review_critic=ReviseCritic(),  # type: ignore[arg-type]
        critic_enabled=True,
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    pipeline._test = RecordingAgent()  # noqa: SLF001
    pipeline._report = ImplicitRevisionReportAgent()  # noqa: SLF001

    _, result = await pipeline.run(_event())

    original = result.persisted_findings[0]
    revised_candidate = result.persisted_findings[2]
    published = result.persisted_findings[3]
    assert revised_candidate.revision_of == original.finding_uid
    assert revised_candidate.finding_uid == published.finding_uid
    assert published.revision_of == original.finding_uid


@pytest.mark.asyncio
async def test_critic_kill_switch_returns_r0_even_when_agent_is_injected(
    tmp_path: Path,
) -> None:
    class _CountingDropCritic(DropCritic):
        def __init__(self) -> None:
            self.calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            return await super().review(**kwargs)

    critic = _CountingDropCritic()
    workspace = FakeWorkspace(path=tmp_path, verify_command=("python", "-m", "pytest", "-q"))
    pipeline = CodeReviewPipeline(
        github_client=FakeGitHub(),  # type: ignore[arg-type]
        workspace_provisioner=FakeProvisioner(workspace),  # type: ignore[arg-type]
        review_critic=critic,  # type: ignore[arg-type]
        critic_enabled=False,
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    pipeline._test = RecordingAgent()  # noqa: SLF001
    pipeline._report = OneFindingReportAgent()  # noqa: SLF001

    _, result = await pipeline.run(_event())

    assert critic.calls == 0
    assert result.critic_metrics == {}
    assert len(result.report.findings) == 1
    assert [item.status for item in result.persisted_findings] == ["published"]


@pytest.mark.asyncio
async def test_pipeline_critic_failure_retries_then_fails_without_r0_fallback(
    tmp_path: Path,
) -> None:
    class _AlwaysFails:
        def __init__(self) -> None:
            self.calls = 0

        async def review(self, **kwargs):
            self.calls += 1
            raise RuntimeError("model unavailable")

    workspace = FakeWorkspace(path=tmp_path, verify_command=("python", "-m", "pytest", "-q"))
    critic = _AlwaysFails()
    pipeline = CodeReviewPipeline(
        github_client=FakeGitHub(),  # type: ignore[arg-type]
        workspace_provisioner=FakeProvisioner(workspace),  # type: ignore[arg-type]
        review_critic=critic,  # type: ignore[arg-type]
        critic_enabled=True,
    )
    pipeline._triage = RecordingAgent()  # noqa: SLF001
    pipeline._static = RecordingAgent()  # noqa: SLF001
    pipeline._semantic = RecordingAgent()  # noqa: SLF001
    pipeline._test = RecordingAgent()  # noqa: SLF001
    pipeline._report = OneFindingReportAgent()  # noqa: SLF001

    with pytest.raises(RuntimeError, match="after 2 attempts"):
        await pipeline.run(_event())
    assert critic.calls == 2


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


def test_evidence_gate_rejects_blank_strings() -> None:
    assert _has_verifiable_evidence(()) is False
    assert _has_verifiable_evidence(("", "   ")) is False
    assert _has_verifiable_evidence(("plausible because the model says so",)) is False
    assert (
        _has_verifiable_evidence(
            ("diff:app/calc.py:1",),
            finding_file="app/calc.py",
            finding_line=1,
            changed_files=(
                {
                    "filename": "app/calc.py",
                    "patch": "@@ -0,0 +1,1 @@\n+def divide(a, b):",
                },
            ),
        )
        is True
    )
    assert (
        _has_verifiable_evidence(
            ("diff:app/calc.py:2",),
            finding_file="app/calc.py",
            finding_line=1,
            changed_files=(
                {
                    "filename": "app/calc.py",
                    "patch": "@@ -0,0 +1,2 @@\n+one\n+two",
                },
            ),
        )
        is False
    )
    assert (
        _has_verifiable_evidence(
            ("command:pytest: 4 passed",),
            trusted_evidence=("command:pytest: 4 passed",),
        )
        is True
    )
    assert (
        _has_verifiable_evidence(
            ("diff:not-a-real-file.py:999",),
            finding_file="app/calc.py",
            finding_line=1,
            changed_files=(
                {
                    "filename": "app/calc.py",
                    "patch": "@@ -0,0 +1,1 @@\n+def divide(a, b):",
                },
            ),
        )
        is False
    )


def test_evidence_gate_checks_workspace_file_and_line(tmp_path: Path) -> None:
    target = tmp_path / "app" / "calc.py"
    target.parent.mkdir(parents=True)
    target.write_text("first\nsecond\n", encoding="utf-8")

    assert (
        _has_verifiable_evidence(
            ("file:app/calc.py:2",),
            finding_file="app/calc.py",
            finding_line=2,
            workspace_path=tmp_path,
        )
        is True
    )
    assert (
        _has_verifiable_evidence(
            ("file:app/calc.py:99",),
            finding_file="app/calc.py",
            finding_line=2,
            workspace_path=tmp_path,
        )
        is False
    )
    assert (
        _has_verifiable_evidence(
            ("file:app/other.py:1",),
            finding_file="app/calc.py",
            finding_line=1,
            workspace_path=tmp_path,
        )
        is False
    )


def test_trusted_evidence_collector_reads_raw_agent_json() -> None:
    raw = json.dumps(
        {
            "findings": [{"evidence": ["model:invented"]}],
            "tool_evidence": [
                "command:pytest: 4 passed",
                "ruff:E501",
            ],
        }
    )

    assert _collect_trusted_evidence(raw) == (
        "command:pytest: 4 passed",
        "ruff:e501",
    )
