"""Smoke test: verify RAG retrieval is injected into SemanticReviewAgent."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.contract import AgentEnvelope
from apps.code_review_pipeline.agents.semantic_review_agent import SemanticReviewAgent
from apps.code_review_pipeline.rag.retriever import RetrievalResult


@pytest.mark.asyncio
async def test_semantic_rag_smoke_injects_retrieval() -> None:
    # Build a fake envelope with a minimal diff payload.
    envelope = AgentEnvelope(
        trace_id="smoke-rag-test",
        session_id="session-1",
        user_raw_input=None,
        global_summary="",
        agent_local_slot={
            "diff": "diff --git a/app/api/users.py b/app/api/users.py\n+def create_user():\n+    pass\n",
            "pr_title": "feat: add create user endpoint",
            "rag_context": {},
        },
    )

    # Patch retrieve_team_patterns so we can assert it was called
    # and control its output deterministically.
    fake_result = RetrievalResult(
        context="[team pattern] All user endpoints must validate input.",
        chunks=[
            {
                "trace_id": "trace-1",
                "source_id": "pr-shing26-nexus-vibe-1-diff-1",
                "score": 0.92,
                "content": "PR #1: refactor: transform Nexus-Campus to Nexus-Vibe ...",
            }
        ],
        doc_count=1,
    )

    # We'll patch via module-level import path used inside execute()
    import apps.code_review_pipeline.agents.semantic_review_agent as mod
    mod.retrieve_team_patterns = AsyncMock(return_value=fake_result)

    # Stub LLM: we only need to see whether RAG context reached it.
    fake_llm = MagicMock()
    fake_llm.chat = AsyncMock(return_value='{"findings":[],"summary":"ok","recommendation":"approve"}')

    agent = SemanticReviewAgent(llm=fake_llm)
    response = await agent.execute(envelope)

    print("retrieve_team_patterns called:", mod.retrieve_team_patterns.called)
    print("llm.chat called:", fake_llm.chat.called)
    messages = fake_llm.chat.call_args[0][0]
    user_msg = messages[-1]["content"]
    print("user message contains RAG:", "team pattern" in user_msg)
    print("user message contains historical_prs:", "pr-shing26-nexus-vibe-1" in user_msg)
    print("response:", response)
