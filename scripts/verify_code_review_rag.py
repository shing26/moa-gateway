"""Verify the code-review knowledge base against the configured vector store.

The check exercises the same public interfaces used by the PR review pipeline:
knowledge ingestion, embedding generation, and team-pattern retrieval.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from app.agents.contract import AgentEnvelope  # noqa: E402
from apps.code_review_pipeline.rag.knowledge_base import (  # noqa: E402
    KnowledgeDoc,
    build_knowledge_base,
)
from apps.code_review_pipeline.rag.retriever import retrieve_team_patterns  # noqa: E402


SOURCE_ID = "code-review-rag-acceptance"


async def main() -> int:
    knowledge_base = build_knowledge_base()
    count = await knowledge_base.ingest_documents(
        [
            KnowledgeDoc(
                source_type="review_pattern",
                source_id=SOURCE_ID,
                title="database connection policy",
                content="团队数据库访问规范：必须复用连接池，设置连接超时，并记录慢查询。",
                metadata={"suite": "code-review-rag-acceptance"},
            )
        ]
    )
    envelope = AgentEnvelope(
        trace_id="code-review-rag-acceptance",
        session_id="code-review-rag-acceptance",
        user_raw_input="数据库访问为什么必须使用连接池并设置超时？",
        global_summary="",
        agent_local_slot={},
    )
    result = await retrieve_team_patterns(envelope, limit=3)
    found = SOURCE_ID in [item.get("source_id") for item in result.chunks]
    print(f"INGESTED={count} DOC_COUNT={result.doc_count} TARGET_FOUND={found}")
    return 0 if count == 1 and found else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
