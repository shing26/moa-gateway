from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.models.events import MoAEvent
from apps.code_review_pipeline.agents.report_agent import ReportAgent
from apps.code_review_pipeline.agents.review_critic import (
    ReviewCriticAgent,
    ReviewCriticResult,
)
from apps.code_review_pipeline.agents.semantic_review_agent import SemanticReviewAgent
from apps.code_review_pipeline.agents.static_analysis_agent import StaticAnalysisAgent
from apps.code_review_pipeline.agents.coverage_agent import TestCoverageAgent
from apps.code_review_pipeline.agents.triage_agent import TriageAgent
from apps.code_review_pipeline.rag.retriever import retrieve_team_patterns
from apps.code_review_pipeline.routing.github_client import GitHubClient, GitHubRepo
from apps.code_review_pipeline.routing.github_provider import build_github_client
from apps.code_review_pipeline.routing.github_webhook_adapter import (
    PRFetchError,
    UnsupportedGitHubEvent,
    build_pr_context_from_github,
)
from apps.code_review_pipeline.schemas.feedback import (
    FINDING_STATUS_CANDIDATE,
    FINDING_STATUS_DROPPED,
    FINDING_STATUS_PUBLISHED,
    FINDING_STATUS_SUPERSEDED,
    persisted_finding_from_result,
)
from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding, PipelineResult
from apps.code_review_pipeline.schemas.pr_context import PRContext
from apps.code_review_pipeline.workspace import (
    GitHubArchiveWorkspaceProvisioner,
    ReviewWorkspace,
)

logger = logging.getLogger("moa.code_review.pipeline")

_MODEL_DIFF_LIMIT = 12_000
_MODEL_PATCH_LIMIT = 4_000


class CodeReviewPipeline:
    def __init__(
        self,
        *,
        github_client: GitHubClient,
        workspace_provisioner: Any | None = None,
        review_critic: Any | None = None,
        critic_enabled: bool | None = None,
        llm: Any | None = None,
    ) -> None:
        self._github_client = github_client
        self._workspace_provisioner = workspace_provisioner
        self._llm = llm
        self._triage = TriageAgent(llm)
        self._static = StaticAnalysisAgent(llm)
        self._semantic = SemanticReviewAgent(llm)
        self._test = TestCoverageAgent(llm)
        self._report = ReportAgent(llm)
        self._review_critic = review_critic
        self._critic_enabled = bool(
            review_critic is not None
            and (critic_enabled if critic_enabled is not None else True)
        )

    async def run(self, event: MoAEvent) -> tuple[PRContext, PipelineResult]:
        body = dict(event.context or {})
        try:
            pr = await build_pr_context_from_github(self._github_client, body)
        except UnsupportedGitHubEvent as exc:
            logger.info("skip unsupported pr event: %s", exc)
            raise

        logger.info("pr loaded repo=%s pr=%s files=%d", pr.repo, pr.pr_number, len(pr.changed_files))

        workspace = await self._prepare_workspace(pr)
        try:
            return await self._run_with_workspace(event, pr, workspace)
        finally:
            await self._cleanup_workspace(workspace)

    async def run_prepared_pr(
        self,
        event: MoAEvent,
        pr: PRContext,
        *,
        workspace: ReviewWorkspace | None = None,
    ) -> tuple[PRContext, PipelineResult]:
        """Run the review chain for an already materialized PR/diff context.

        This is the same path used after GitHub webhook ingestion. Offline
        evaluation uses it so controlled cases can run without fabricating a
        GitHub API response or writing to a third-party repository.
        """
        try:
            return await self._run_with_workspace(event, pr, workspace)
        finally:
            await self._cleanup_workspace(workspace)

    async def _prepare_workspace(self, pr: PRContext) -> ReviewWorkspace | None:
        if self._workspace_provisioner is None:
            return None
        try:
            return await self._workspace_provisioner.prepare(pr)
        except Exception as exc:
            logger.warning("workspace provisioning failed for %s#%s: %s", pr.repo, pr.pr_number, exc)
            return ReviewWorkspace(
                path=None,
                error=f"workspace provision failed: {exc}",
            )

    async def _cleanup_workspace(self, workspace: ReviewWorkspace | None) -> None:
        if workspace is None or self._workspace_provisioner is None:
            return
        try:
            await self._workspace_provisioner.cleanup(workspace)
        except Exception as exc:
            logger.warning("workspace cleanup failed: %s", exc)

    async def _run_with_workspace(
        self,
        event: MoAEvent,
        pr: PRContext,
        workspace: ReviewWorkspace | None,
    ) -> tuple[PRContext, PipelineResult]:
        # Build a shared diff payload for agents.
        # Week3: enrich semantic review with team-specific RAG context.
        diff_payload = {
            "pr_title": pr.title,
            "diff": "\n".join(f.patch or "" for f in pr.changed_files if f.patch),
            "changed_files": [
                {
                    "filename": f.filename,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "changes": f.changes,
                    "patch": f.patch,
                }
                for f in pr.changed_files
            ],
            "changed_python_files": [
                {
                    "filename": f.filename,
                    "content": f.content or f.patch or "",
                }
                for f in pr.changed_files
                if f.filename.endswith(".py") and f.content
            ],
        }

        triage_output = await self._triage.execute(_envelope_from_event(event, diff_payload, agent="triage"))
        static_output = await self._static.execute(_envelope_from_event(event, diff_payload, agent="static_analysis"))

        # Retrieve team-specific patterns for semantic review.
        semantic_envelope = _envelope_from_event(event, diff_payload, agent="semantic_review")
        try:
            rag_result = await retrieve_team_patterns(semantic_envelope, limit=5)
        except Exception as exc:
            logger.warning("RAG retrieval failed: %s", exc)
            rag_result = None

        if rag_result and rag_result.context:
            semantic_envelope.agent_local_slot["rag_context"] = {
                "patterns": [rag_result.context],
                "historical_prs": [
                    {
                        "source_id": item.get("source_id", ""),
                        "score": item.get("score", 0.0),
                        "content": item.get("content", "")[:500],
                    }
                    for item in rag_result.chunks
                ],
            }

        semantic_output = await self._semantic.execute(semantic_envelope)
        test_output = await self._test.execute(
            _envelope_from_event(
                event,
                diff_payload,
                agent="test_coverage",
                workspace=workspace,
            )
        )
        trusted_evidence = _collect_trusted_evidence(static_output, test_output)
        if rag_result is not None:
            trusted_evidence += tuple(
                f"rag:{item.get('source_id', '')}"
                for item in rag_result.chunks
                if str(item.get("source_id") or "").strip()
            )
        report_inputs = {
            "triage": _safe_json(triage_output),
            "static_analysis": _safe_json(static_output),
            "semantic_review": _safe_json(semantic_output),
            "test_coverage": _safe_json(test_output),
        }
        report_output = await self._report.execute(
            _envelope_from_event(
                event,
                diff_payload,
                agent="report",
                report_inputs=report_inputs,
            )
        )

        trace_id = event.trace_id
        triage_result = _agent_result("triage", trace_id, triage_output)
        static_result = _agent_result("static_analysis", trace_id, static_output)
        semantic_result = _agent_result("semantic_review", trace_id, semantic_output)
        test_result = _agent_result("test_coverage", trace_id, test_output)
        report_result = _agent_result("report", trace_id, report_output)
        persisted_findings: tuple[Any, ...] = ()
        critic_metrics: dict[str, Any] = {}
        if self._critic_enabled and self._review_critic is not None:
            report_result, persisted_findings, critic_metrics = await self._apply_review_critic(
                event=event,
                pr=pr,
                diff_payload=diff_payload,
                workspace=workspace,
                report_inputs=report_inputs,
                report_result=report_result,
                trusted_evidence=trusted_evidence,
            )
        elif report_result.findings:
            persisted_findings = tuple(
                persisted_finding_from_result(
                    trace_id=event.trace_id,
                    agent="report",
                    finding=_with_default_sources(finding),
                    status=FINDING_STATUS_PUBLISHED,
                )
                for finding in report_result.findings
            )

        overall_need_human_review = (
            triage_result.need_human_review
            or static_result.need_human_review
            or semantic_result.need_human_review
            or test_result.need_human_review
            or report_result.need_human_review
        )

        pipeline_result = PipelineResult(
            trace_id=trace_id,
            pr=pr,
            triage=triage_result,
            static_analysis=static_result,
            semantic_review=semantic_result,
            test_coverage=test_result,
            report=report_result,
            overall_need_human_review=overall_need_human_review,
            persisted_findings=persisted_findings,
            critic_metrics=critic_metrics,
        )
        return pr, pipeline_result

    async def _apply_review_critic(
        self,
        *,
        event: MoAEvent,
        pr: PRContext,
        diff_payload: dict[str, Any],
        workspace: ReviewWorkspace | None,
        report_inputs: dict[str, Any],
        report_result: AgentFindingResult,
        trusted_evidence: tuple[str, ...],
    ) -> tuple[AgentFindingResult, tuple[Any, ...], dict[str, Any]]:
        candidates = tuple(
            persisted_finding_from_result(
                trace_id=event.trace_id,
                agent="report",
                finding=_with_default_sources(finding),
                status="candidate",
            )
            for finding in report_result.findings
        )
        if not candidates:
            return report_result, (), {}
        critic = self._review_critic
        assert critic is not None
        critic_result = await self._review_critic_with_retry(
            critic,
            trace_id=event.trace_id,
            pr_title=pr.title,
            diff=diff_payload.get("diff", ""),
            findings=candidates,
            workspace_path=getattr(workspace, "path", None),
            verify_command=getattr(workspace, "verify_command", ()) or (),
        )
        if critic_result.tool_outputs:
            trusted_evidence += tuple(
                f"command:{value}"
                for value in critic_result.tool_outputs.values()
                if str(value).strip()
            )
        by_uid = {item.finding_uid: item for item in critic_result.verdicts}
        published: list[Finding] = []
        persisted: list[Any] = []
        revise_candidates: list[tuple[Any, Any]] = []
        for candidate, raw in zip(candidates, report_result.findings, strict=True):
            persisted.append(candidate)
            decision = by_uid[candidate.finding_uid]
            if decision.decision == "keep":
                if not _has_verifiable_evidence(
                    raw.evidence,
                    finding_file=raw.file,
                    finding_line=raw.line,
                    workspace_path=getattr(workspace, "path", None),
                    changed_files=tuple(diff_payload.get("changed_files", ())),
                    trusted_evidence=trusted_evidence,
                ):
                    persisted.append(
                        replace(
                            candidate,
                            status=FINDING_STATUS_DROPPED,
                            critic_decision="keep",
                            critic_reason="evidence_gate: insufficient_evidence",
                        )
                    )
                    continue
                published.append(raw)
                persisted.append(
                    replace(
                        candidate,
                        status=FINDING_STATUS_PUBLISHED,
                        critic_decision="keep",
                        critic_reason=decision.reason,
                        evidence_verified=True,
                    )
                )
            elif decision.decision == "drop":
                persisted.append(
                    replace(
                        candidate,
                        status=FINDING_STATUS_DROPPED,
                        critic_decision="drop",
                        critic_reason=decision.reason,
                    )
                )
            else:
                revise_candidates.append((candidate, decision))
                persisted.append(
                    replace(
                        candidate,
                        status=FINDING_STATUS_SUPERSEDED,
                        critic_decision="revise",
                        critic_reason=decision.reason,
                    )
                )

        if revise_candidates:
            revised_report, revised_persisted = await self._revise_findings(
                event=event,
                pr=pr,
                diff_payload=diff_payload,
                report_inputs=report_inputs,
                revise_candidates=tuple(revise_candidates),
                workspace=workspace,
                trusted_evidence=trusted_evidence,
            )
            published.extend(revised_report.findings)
            persisted.extend(revised_persisted)

        final_report = _report_from_findings(report_result, tuple(published))
        return final_report, tuple(persisted), dict(critic_result.metrics or {})

    async def _review_critic_with_retry(
        self,
        critic: Any,
        **kwargs: Any,
    ) -> ReviewCriticResult:
        """Run the Critic with one bounded retry.

        A Critic failure must not silently turn R1 into R0: dropping the
        backedge would make the evaluation labels claim a topology that did
        not actually run. The retry gives transient tool/model failures one
        chance; after that the worker task fails and can be requeued.
        """
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                result = await critic.review(**kwargs)
                if not isinstance(result, ReviewCriticResult):
                    raise TypeError("review critic returned an unexpected result")
                return result
            except Exception as exc:  # noqa: BLE001 - surfaced after bounded retry
                last_error = exc
                logger.warning("review critic attempt %d failed: %s", attempt + 1, exc)
        raise RuntimeError(f"review critic failed after 2 attempts: {last_error}") from last_error

    async def _revise_findings(
        self,
        *,
        event: MoAEvent,
        pr: PRContext,
        diff_payload: dict[str, Any],
        report_inputs: dict[str, Any],
        revise_candidates: tuple[tuple[Any, Any], ...],
        workspace: ReviewWorkspace | None,
        trusted_evidence: tuple[str, ...],
    ) -> tuple[AgentFindingResult, tuple[Any, ...]]:
        """Run the single allowed ReportAgent backedge for Critic revisions."""
        candidate_payload = [
            {
                "finding_uid": candidate.finding_uid,
                "id": candidate.raw_finding_id,
                "severity": candidate.severity,
                "category": candidate.category,
                "file": candidate.file,
                "line": candidate.line,
                "title": candidate.title,
                "description": candidate.description,
                "suggestion": candidate.suggestion,
                "confidence": candidate.confidence,
                "evidence": list(candidate.evidence),
                "source_agents": list(candidate.source_agents),
            }
            for candidate, _decision in revise_candidates
        ]
        instructions = [
            {
                "finding_uid": candidate.finding_uid,
                "reason": decision.reason,
                "instructions": decision.instructions,
            }
            for candidate, decision in revise_candidates
        ]
        revised = await self._report.execute(
            _envelope_from_event(
                event,
                diff_payload,
                agent="report",
                report_inputs=report_inputs,
                candidate_findings=candidate_payload,
                critic_instructions=instructions,
            )
        )
        if not isinstance(revised, AgentFindingResult):
            raise TypeError("report revision returned an unexpected result")

        by_uid = {candidate.finding_uid: candidate for candidate, _ in revise_candidates}
        used: set[str] = set()
        persisted: list[Any] = []
        revised_findings: list[Finding] = []
        for finding in revised.findings:
            source_uid = finding.revision_of
            if source_uid not in by_uid or source_uid in used:
                source_uid = _match_revision_source(finding, by_uid, used)
            if source_uid is None:
                # An unanchored rewrite cannot be attributed to a candidate and
                # therefore must not become a published finding.
                persisted.append(
                    persisted_finding_from_result(
                        trace_id=event.trace_id,
                        agent="report",
                        finding=finding,
                        status=FINDING_STATUS_CANDIDATE,
                    )
                )
                persisted.append(
                    persisted_finding_from_result(
                        trace_id=event.trace_id,
                        agent="report",
                        finding=finding,
                        status=FINDING_STATUS_DROPPED,
                        critic_decision="revise",
                        critic_reason="revision lineage missing",
                    )
                )
                continue
            used.add(source_uid)
            persisted.append(
                persisted_finding_from_result(
                    trace_id=event.trace_id,
                    agent="report",
                    finding=finding,
                    status=FINDING_STATUS_CANDIDATE,
                    revision_of=source_uid,
                )
            )
            original = by_uid[source_uid]
            merged = replace(
                finding,
                revision_of=source_uid,
                source_agents=tuple(
                    sorted(
                        {
                            *(finding.source_agents or ()),
                            *(original.source_agents or ()),
                            "report",
                        }
                    )
                ),
            )
            if not _has_verifiable_evidence(
                merged.evidence,
                finding_file=merged.file,
                finding_line=merged.line,
                workspace_path=getattr(workspace, "path", None),
                changed_files=tuple(diff_payload.get("changed_files", ())),
                trusted_evidence=trusted_evidence,
            ):
                persisted.append(
                    persisted_finding_from_result(
                        trace_id=event.trace_id,
                        agent="report",
                        finding=merged,
                        status=FINDING_STATUS_DROPPED,
                        critic_decision="revise",
                        critic_reason="insufficient_evidence",
                    )
                )
                continue
            revised_findings.append(merged)
            persisted.append(
                persisted_finding_from_result(
                    trace_id=event.trace_id,
                    agent="report",
                    finding=merged,
                    status=FINDING_STATUS_PUBLISHED,
                    critic_decision="revise",
                    critic_reason="revised by report worker",
                    evidence_verified=True,
                )
            )
        return (
            AgentFindingResult(
                agent="report",
                trace_id=revised.trace_id,
                findings=tuple(revised_findings),
                summary=revised.summary,
                recommendation=revised.recommendation,
                need_human_review=revised.need_human_review,
                stats=revised.stats,
            ),
            tuple(persisted),
        )

    @staticmethod
    def from_env() -> CodeReviewPipeline:
        # 选型交给 github_provider：真 GitHub / 离线 fixture 只在那一个文件里判断。
        from app.config import settings

        client = build_github_client()
        return CodeReviewPipeline(
            github_client=client,
            workspace_provisioner=GitHubArchiveWorkspaceProvisioner(client),
            review_critic=ReviewCriticAgent(),
            critic_enabled=settings.code_review_critic_enabled,
        )


def _envelope_from_event(
    event: MoAEvent,
    diff_payload: dict[str, Any],
    *,
    agent: str,
    rag_context: dict[str, Any] | None = None,
    report_inputs: dict[str, Any] | None = None,
    workspace: ReviewWorkspace | None = None,
    candidate_findings: list[dict[str, Any]] | None = None,
    critic_instructions: list[dict[str, Any]] | None = None,
) -> AgentEnvelope:
    slot: dict[str, Any] = {
        "agent": agent,
        "diff": diff_payload.get("diff", ""),
        "pr_title": diff_payload.get("pr_title", ""),
        "changed_files": diff_payload.get("changed_files", []),
    }
    if rag_context is not None:
        slot["rag_context"] = rag_context
    if report_inputs is not None:
        slot["report_inputs"] = report_inputs
    if candidate_findings is not None:
        slot["candidate_findings"] = candidate_findings
    if critic_instructions is not None:
        slot["critic_instructions"] = critic_instructions
    if workspace is not None:
        path = getattr(workspace, "path", None)
        if path:
            slot["workspace_path"] = str(path)
        verify_command = getattr(workspace, "verify_command", ()) or ()
        if verify_command:
            slot["verify_command"] = [str(token) for token in verify_command]
        error = str(getattr(workspace, "error", "") or "").strip()
        if error:
            slot["workspace_error"] = error

    from app.agents.contract import AgentEnvelope
    return AgentEnvelope(
        trace_id=event.trace_id,
        session_id=event.session_id,
        # The code-review pipeline is driven by a GitHub event, not by a chat
        # utterance. Keep the diff available even when a caller supplies a
        # short instruction, but bound the model-facing copy so a large PR
        # cannot silently consume the whole context window.
        user_raw_input=_build_model_review_input(diff_payload, event.text or ""),
        global_summary=event.text or "",
        agent_local_slot=slot,
        history=(),
    )


def _build_model_review_input(diff_payload: dict[str, Any], user_text: str) -> str:
    changed_files = []
    for item in diff_payload.get("changed_files", [])[:50]:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        patch = str(row.get("patch") or "")
        if len(patch) > _MODEL_PATCH_LIMIT:
            row["patch"] = patch[:_MODEL_PATCH_LIMIT] + "\n…（patch 已截断）"
        changed_files.append(row)
    payload: dict[str, Any] = {
        "pr_title": diff_payload.get("pr_title", ""),
        "diff": str(diff_payload.get("diff") or "")[:_MODEL_DIFF_LIMIT],
        "changed_files": changed_files,
    }
    if user_text.strip():
        payload["user_text"] = user_text
    return json.dumps(payload, ensure_ascii=False)


def _safe_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text) if text else {}
    except Exception:
        return {"raw": text}


def _with_default_sources(finding: Finding) -> Finding:
    if finding.source_agents:
        return finding
    return replace(finding, source_agents=("report",))


def _collect_trusted_evidence(*sections: Any) -> tuple[str, ...]:
    """Collect evidence emitted by deterministic tools, not by the Report model."""
    evidence: set[str] = set()
    for section in sections:
        payload = _agent_payload(section)
        values = (
            payload.get("tool_evidence", ())
            if isinstance(payload, dict)
            else getattr(payload, "tool_evidence", ())
        )
        for item in values or ():
            normalized = str(item).strip().lower()
            if normalized:
                evidence.add(normalized)
    return tuple(sorted(evidence))


def _agent_payload(section: Any) -> Any:
    """Parse raw JSON agent output while leaving parsed results unchanged."""
    if isinstance(section, str):
        try:
            return json.loads(section)
        except (TypeError, ValueError):
            return {}
    return section


def _has_verifiable_evidence(
    evidence: tuple[str, ...],
    *,
    finding_file: str = "",
    finding_line: int = 0,
    workspace_path: Path | str | None = None,
    changed_files: tuple[dict[str, Any], ...] = (),
    trusted_evidence: tuple[str, ...] = (),
) -> bool:
    """Require evidence that resolves to the diff, workspace, or trusted tool output."""
    trusted = {str(item).strip().lower() for item in trusted_evidence if str(item).strip()}
    diff_lines = _diff_covered_lines(changed_files)
    for item in evidence:
        normalized = str(item).strip()
        lowered = normalized.lower()
        if lowered in trusted:
            return True
        prefix, separator, payload = normalized.partition(":")
        if not separator or not payload.strip():
            continue
        kind = prefix.strip().lower()
        if kind == "diff":
            location = _parse_evidence_location(payload)
            if location is None:
                continue
            path, line = location
            if _normalized_repo_path(path) == _normalized_repo_path(finding_file):
                if finding_line > 0 and line != finding_line:
                    continue
                if line in diff_lines.get(_normalized_repo_path(path), set()):
                    return True
        elif kind == "file":
            location = _parse_evidence_location(payload)
            if location is None:
                continue
            path, line = location
            if _normalized_repo_path(path) != _normalized_repo_path(finding_file):
                continue
            if finding_line > 0 and line != finding_line:
                continue
            if _workspace_file_line_exists(workspace_path, payload):
                return True
    return False


def _parse_evidence_location(payload: str) -> tuple[str, int] | None:
    path, separator, raw_line = payload.strip().rpartition(":")
    if not separator or not path.strip():
        return None
    try:
        line = int(raw_line)
    except ValueError:
        return None
    return path.strip(), line


def _normalized_repo_path(value: str) -> str:
    normalized = str(value).replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _diff_covered_lines(
    changed_files: tuple[dict[str, Any], ...],
) -> dict[str, set[int]]:
    covered: dict[str, set[int]] = {}
    for item in changed_files:
        if not isinstance(item, dict):
            continue
        path = _normalized_repo_path(str(item.get("filename") or ""))
        patch = str(item.get("patch") or "")
        if not path or not patch:
            continue
        lines = covered.setdefault(path, set())
        old_line = 0
        new_line = 0
        for raw in patch.splitlines():
            if raw.startswith("@@"):
                match = re.search(r"-(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?", raw)
                if match:
                    old_line = int(match.group(1))
                    new_line = int(match.group(2))
                continue
            if old_line <= 0 and new_line <= 0:
                continue
            if raw.startswith("+++") or raw.startswith("---"):
                continue
            if raw.startswith("+"):
                if new_line > 0:
                    lines.add(new_line)
                new_line += 1
            elif raw.startswith("-"):
                if old_line > 0:
                    lines.add(old_line)
                old_line += 1
                continue
            else:
                if new_line > 0:
                    lines.add(new_line)
                if old_line > 0:
                    lines.add(old_line)
                old_line += 1
                new_line += 1
    return covered


def _workspace_file_line_exists(
    workspace_path: Path | str | None,
    payload: str,
) -> bool:
    if workspace_path is None:
        return False
    location = _parse_evidence_location(payload)
    if location is None:
        return False
    path, line = location
    root = Path(workspace_path).resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or line <= 0 or not candidate.is_file():
        return False
    try:
        return line <= len(candidate.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return False


def _report_from_findings(
    report: AgentFindingResult,
    findings: tuple[Finding, ...],
) -> AgentFindingResult:
    critical_or_high = sum(
        1 for item in findings if item.severity.lower() in {"critical", "high"}
    )
    medium = sum(1 for item in findings if item.severity.lower() == "medium")
    recommendation = (
        "request_changes"
        if critical_or_high >= 1 or medium >= 3
        else ("approve" if not findings else report.recommendation)
    )
    dropped = max(0, len(report.findings) - len(findings))
    summary = report.summary
    if dropped:
        suffix = f" Review Critic 保留 {len(findings)} 条，剔除 {dropped} 条。"
        summary = f"{summary.rstrip()}{suffix}".strip()
    return AgentFindingResult(
        agent=report.agent,
        trace_id=report.trace_id,
        findings=findings,
        summary=summary,
        recommendation=recommendation,
        need_human_review=bool(critical_or_high >= 1 or medium >= 3),
        stats=report.stats,
    )


def _match_revision_source(
    finding: Finding,
    candidates: dict[str, Any],
    used: set[str],
) -> str | None:
    """Find a revision lineage when the model omitted ``revision_of``.

    The explicit field is authoritative. The fallback is deliberately narrow:
    first match the original id, then exact location/category. If the model
    rewrites too much to identify the source, the finding is left unanchored
    rather than guessed into a human-visible slot.
    """
    for uid, candidate in candidates.items():
        if uid in used:
            continue
        if finding.id and finding.id == candidate.raw_finding_id:
            return uid
    for uid, candidate in candidates.items():
        if uid in used:
            continue
        if (
            finding.file == candidate.file
            and int(finding.line) == int(candidate.line)
            and finding.category == candidate.category
        ):
            return uid
    return None


def _agent_result(agent: str, trace_id: str, raw: Any) -> AgentFindingResult:
    """把各agent 的产出归一成 AgentFindingResult。

    两种输入形态：
    - ``AgentFindingResult``：已经是结构化结果的（例如 ``ReportAgent.execute`` 内部
      ``_parse_report`` 已按 severity 推断出 ``need_human_review``）。**必须原样透传**。
    - ``str``：其余agent 返回的 JSON 文本，在此解析。

    历史缺陷：此前本函数只接受 str，把已经算好的 ``AgentFindingResult`` 也丢进
    ``json.loads()``，异常后走 ``{"raw": ...}`` 分支——findings 全丢、summary 空、
    ``need_human_review`` 恒为 False。于是 ``ReportAgent._infer_hitl`` 依据
    critical/high/medium 推断出的结果被静默丢弃，五个agent 无论报多严重的问题
    都不会触发人工复核。
    """
    if isinstance(raw, AgentFindingResult):
        return raw
    data = _safe_json(raw)
    # _safe_json 对无法解析的文本返回 {"raw": ...} 而不抛异常。若不显式识别，
    # 一次"agent 返回垃圾"会被静默当成"零发现 + 不需复核"——最危险的一种失败：
    # 上游模型出问题，下游审计却显示一切正常。解析失败必须升级为人工复核。
    if "raw" in data and "findings" not in data:
        logger.warning("%s returned unparsable payload; escalating to human review", agent)
        return AgentFindingResult(
            agent=agent,
            trace_id=trace_id,
            findings=(),
            summary="parse failed",
            recommendation="comment",
            need_human_review=True,
        )
    try:
        findings = []
        for item in data.get("findings", []):
            findings.append(Finding(
                id=str(item.get("id", f"{agent}-finding")),
                severity=str(item.get("severity", "medium")),
                category=str(item.get("category", "general")),
                file=str(item.get("file", "")),
                line=int(item.get("line", 0)),
                title=str(item.get("title", "")),
                description=str(item.get("description", "")),
                suggestion=str(item.get("suggestion", "")),
                confidence=float(item.get("confidence", 0.0)),
                team_specific=bool(item.get("team_specific", False)),
                evidence=tuple(item.get("evidence", [])),
                source_agents=tuple(item.get("source_agents", []) or (agent,)),
            ))
        return AgentFindingResult(
            agent=agent,
            trace_id=trace_id,
            findings=tuple(findings),
            summary=str(data.get("summary", "")),
            recommendation=str(data.get("recommendation", "comment")),
            need_human_review=bool(data.get("need_human_review", False)),
        )
    except Exception as exc:
        logger.warning("parse %s result failed: %s", agent, exc)
        return AgentFindingResult(
            agent=agent,
            trace_id=trace_id,
            findings=(),
            summary="parse failed",
            recommendation="comment",
            need_human_review=True,
        )
