from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents.provider import ChatResult
from apps.code_review_pipeline.agents.review_critic import (
    ReviewCriticAgent,
    parse_review_critic_result,
)
from apps.code_review_pipeline.schemas.feedback import persisted_finding_from_result
from apps.code_review_pipeline.schemas.pipeline import Finding


def _candidate():
    return persisted_finding_from_result(
        trace_id="cr_owner/repo#7@abc",
        agent="report",
        finding=Finding(
            id="final-001",
            severity="high",
            category="logic",
            file="app/service.py",
            line=10,
            title="Wrong retry boundary",
            description="The last retry is skipped.",
            suggestion="Use an inclusive boundary.",
            confidence=0.9,
            evidence=("diff:app/service.py:10",),
            source_agents=("semantic_review", "report"),
        ),
        status="candidate",
    )


def test_critic_revise_verdict_requires_actionable_instructions() -> None:
    finding = _candidate()
    raw = json.dumps(
        {
            "verdicts": [
                {
                    "finding_uid": finding.finding_uid,
                    "decision": "revise",
                    "reason": "The description overstates the impact.",
                    "instructions": "",
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="instructions"):
        parse_review_critic_result(raw, (finding,))


@pytest.mark.asyncio
async def test_review_critic_evaluates_each_candidate_finding() -> None:
    finding = _candidate()

    class _LLM:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []
            self.last_metrics = {
                "cost_usd": 0.002,
                "prompt_tokens": 100,
                "completion_tokens": 20,
            }

        async def chat_with_tools(self, messages, tools, **kwargs):
            self.messages = messages
            return ChatResult(
                messages=messages,
                content=json.dumps(
                    {
                        "verdicts": [
                            {
                                "finding_uid": finding.finding_uid,
                                "decision": "keep",
                                "reason": "The diff supports the claim.",
                            }
                        ]
                    }
                ),
                tool_calls=[],
            )

    llm = _LLM()
    agent = ReviewCriticAgent(llm=llm)

    result = await agent.review(
        trace_id=finding.trace_id,
        pr_title="Fix retry boundary",
        diff="+ retry_until = 3",
        findings=(finding,),
    )

    assert result.verdicts[0].decision == "keep"
    assert result.metrics["cost_usd"] == 0.002
    assert finding.finding_uid in str(llm.messages[-1]["content"])


@pytest.mark.asyncio
async def test_review_critic_can_read_files_without_a_verify_command(
    tmp_path: Path,
) -> None:
    finding = _candidate()

    class _LLM:
        def __init__(self) -> None:
            self.tool_names: list[str] = []
            self.last_metrics: dict[str, object] = {}

        async def chat_with_tools(self, messages, tools, **kwargs):
            self.tool_names = [
                str((tool.get("function") or {}).get("name", ""))
                for tool in tools
            ]
            return ChatResult(
                messages=messages,
                content=json.dumps(
                    {
                        "verdicts": [
                            {
                                "finding_uid": finding.finding_uid,
                                "decision": "keep",
                                "reason": "read_file confirms the source.",
                            }
                        ]
                    }
                ),
                tool_calls=[],
            )

    llm = _LLM()
    result = await ReviewCriticAgent(llm=llm).review(
        trace_id=finding.trace_id,
        pr_title="Fix retry boundary",
        diff="+ retry_until = 3",
        findings=(finding,),
        workspace_path=tmp_path,
        verify_command=(),
    )

    assert llm.tool_names == ["read_file"]
    assert result.verdicts[0].decision == "keep"


@pytest.mark.asyncio
async def test_review_critic_metrics_include_every_tool_loop_call() -> None:
    finding = _candidate()

    class _LLM:
        def __init__(self) -> None:
            self.calls = 0
            self.last_metrics: dict[str, object] = {}

        async def chat_with_tools(self, messages, tools, **kwargs):
            self.calls += 1
            self.last_metrics = {
                "model_used": "fixed-model",
                "cost_usd": 0.001,
                "llm_latency_ms": 10,
                "prompt_tokens": 100,
                "completion_tokens": 20,
            }
            if self.calls == 1:
                return ChatResult(
                    messages=messages,
                    content="",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "not_available",
                                "arguments": "{}",
                            },
                        }
                    ],
                )
            return ChatResult(
                messages=messages,
                content=json.dumps(
                    {
                        "verdicts": [
                            {
                                "finding_uid": finding.finding_uid,
                                "decision": "keep",
                                "reason": "The diff supports the claim.",
                            }
                        ]
                    }
                ),
                tool_calls=[],
            )

    result = await ReviewCriticAgent(llm=_LLM()).review(
        trace_id=finding.trace_id,
        pr_title="Fix retry boundary",
        diff="+ retry_until = 3",
        findings=(finding,),
    )

    assert result.metrics["calls"] == 2
    assert result.metrics["cost_usd"] == pytest.approx(0.002)
    assert result.metrics["prompt_tokens"] == 200
    assert result.metrics["completion_tokens"] == 40
