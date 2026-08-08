from __future__ import annotations

import logging
from typing import Any

from app.models.events import MoAEvent
from apps.code_review_pipeline.agents.report_agent import ReportAgent
from apps.code_review_pipeline.agents.semantic_review_agent import SemanticReviewAgent
from apps.code_review_pipeline.agents.static_analysis_agent import StaticAnalysisAgent
from apps.code_review_pipeline.agents.test_coverage_agent import TestCoverageAgent
from apps.code_review_pipeline.agents.triage_agent import TriageAgent
from apps.code_review_pipeline.rag.retriever import retrieve_team_patterns
from apps.code_review_pipeline.routing.github_client import GitHubClient, GitHubRepo
from apps.code_review_pipeline.routing.github_webhook_adapter import (
    PRFetchError,
    UnsupportedGitHubEvent,
    build_pr_context_from_github,
)
from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding, PipelineResult
from apps.code_review_pipeline.schemas.pr_context import PRContext

logger = logging.getLogger("moa.code_review.pipeline")


class CodeReviewPipeline:
    def __init__(self, *, github_client: GitHubClient) -> None:
        self._github_client = github_client
        self._triage = TriageAgent()
        self._static = StaticAnalysisAgent()
        self._semantic = SemanticReviewAgent()
        self._test = TestCoverageAgent()
        self._report = ReportAgent()

    async def run(self, event: MoAEvent) -> tuple[PRContext, PipelineResult]:
        body = dict(event.context or {})
        try:
            pr = await build_pr_context_from_github(self._github_client, body)
        except UnsupportedGitHubEvent as exc:
            logger.info("skip unsupported pr event: %s", exc)
            raise

        logger.info("pr loaded repo=%s pr=%s files=%d", pr.repo, pr.pr_number, len(pr.changed_files))

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
        test_output = await self._test.execute(_envelope_from_event(event, diff_payload, agent="test_coverage"))
        report_output = await self._report.execute(
            _envelope_from_event(event, diff_payload, agent="report", report_inputs={
                "triage": _safe_json(triage_output),
                "static_analysis": _safe_json(static_output),
                "semantic_review": _safe_json(semantic_output),
                "test_coverage": _safe_json(test_output),
            })
        )

        trace_id = event.trace_id
        triage_result = _agent_result("triage", trace_id, triage_output)
        static_result = _agent_result("static_analysis", trace_id, static_output)
        semantic_result = _agent_result("semantic_review", trace_id, semantic_output)
        test_result = _agent_result("test_coverage", trace_id, test_output)
        report_result = _agent_result("report", trace_id, report_output)

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
        )
        return pr, pipeline_result

    @staticmethod
    def from_env() -> CodeReviewPipeline:
        return CodeReviewPipeline(github_client=GitHubClient.from_env())


def _envelope_from_event(event: MoAEvent, diff_payload: dict[str, Any], *, agent: str, rag_context: dict[str, Any] | None = None, report_inputs: dict[str, Any] | None = None) -> AgentEnvelope:
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

    from app.agents.contract import AgentEnvelope
    return AgentEnvelope(
        trace_id=event.trace_id,
        session_id=event.session_id,
        user_raw_input=event.text or diff_payload.get("pr_title", ""),
        global_summary=event.text or "",
        agent_local_slot=slot,
        history=(),
    )


def _safe_json(text: str) -> dict[str, Any]:
    try:
        import json
        return json.loads(text) if text else {}
    except Exception:
        return {"raw": text}


def _agent_result(agent: str, trace_id: str, raw: str) -> AgentFindingResult:
    try:
        data = _safe_json(raw)
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
            ))
        return AgentFindingResult(
            agent=agent,
            trace_id=trace_id,
            findings=tuple(findings),
            summary=str(data.get("summary", "")),
            recommendation=str(data.get("recommendation", "comment")),
            need_human_review=False,
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
