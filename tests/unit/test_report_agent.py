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


def test_report_agent_receives_critic_revision_instructions() -> None:
    class CapturingLLM:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = []

        async def chat(self, messages: list[dict[str, str]]) -> str:
            self.messages = messages
            return json.dumps(
                {
                    "findings": [],
                    "summary": "revised",
                    "recommendation": "comment",
                    "stats": {},
                }
            )

    llm = CapturingLLM()
    agent = ReportAgent(llm=llm)
    envelope = AgentEnvelope(
        trace_id="t1",
        session_id="s1",
        user_raw_input="",
        global_summary="",
        agent_local_slot={
            "triage": {},
            "static_analysis": {},
            "semantic_review": {},
            "test_coverage": {},
            "pr_title": "",
            "critic_instructions": [
                {
                    "finding_uid": "finding-1",
                    "instructions": "Lower the severity and cite the exact diff line.",
                }
            ],
        },
        history=(),
    )

    asyncio.run(agent.execute(envelope))

    assert "Lower the severity" in llm.messages[1]["content"]


def test_report_agent_consumes_pipeline_report_inputs() -> None:
    class CapturingLLM:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = []

        async def chat(self, messages: list[dict[str, str]]) -> str:
            self.messages = messages
            return json.dumps(
                {
                    "findings": [],
                    "summary": "ok",
                    "recommendation": "approve",
                    "stats": {},
                }
            )

    llm = CapturingLLM()
    envelope = AgentEnvelope(
        trace_id="t1",
        session_id="s1",
        user_raw_input="",
        global_summary="",
        agent_local_slot={
            "report_inputs": {
                "triage": {"priority": "high"},
                "static_analysis": {"findings": [{"id": "static-1"}]},
                "semantic_review": {"findings": [{"id": "semantic-1"}]},
                "test_coverage": {"commands": ["pytest"]},
            }
        },
        history=(),
    )

    asyncio.run(ReportAgent(llm=llm).execute(envelope))

    payload = json.loads(llm.messages[1]["content"])
    assert payload["triage"] == {"priority": "high"}
    assert payload["static_analysis"]["findings"][0]["id"] == "static-1"
    assert payload["semantic_review"]["findings"][0]["id"] == "semantic-1"
    assert payload["test_coverage"]["commands"] == ["pytest"]
