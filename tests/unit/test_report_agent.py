from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from apps.code_review_pipeline.agents.report_agent import ReportAgent
from app.agents.contract import AgentEnvelope
from apps.code_review_pipeline.schemas.pipeline import Finding


def _fake_llm(*, findings: list[dict[str, Any]], summary: str = "ok", recommendation: str = "approve") -> type:
    class FakeLLM:
        async def chat(self, messages: list[dict[str, str]]) -> str:
            return json.dumps({"findings": findings, "summary": summary, "recommendation": recommendation, "stats": {}}, ensure_ascii=False)
    return FakeLLM()


def test_report_agent_parses_llm_output() -> None:
    agent = ReportAgent(llm=_fake_llm(findings=[{"id": "final-001", "severity": "high", "category": "security", "file": "a.py", "line": 1, "title": "x", "description": "d", "suggestion": "s", "confidence": 0.9, "team_specific": False, "source_agents": ["static_analysis"]}], summary="summary", recommendation="request_changes"))
    envelope = AgentEnvelope(trace_id="t1", session_id="s1", user_raw_input="", global_summary="", agent_local_slot={"triage": {}, "static_analysis": {}, "semantic_review": {}, "test_coverage": {}, "pr_title": ""}, history=())
    result = asyncio.run(agent.execute(envelope))
    assert result.findings[0].severity == "high"
    assert result.recommendation == "request_changes"
    assert result.need_human_review is True
