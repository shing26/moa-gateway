"""Compare retrieval quality with and without rerank on the gold set.

    uv run python scripts/compare_rerank.py

Runs the gold set twice against the in-memory store: once with the base RRF
ordering, once with ``DeterministicLexicalReranker`` (a deterministic, offline
reranker). Prints both metric sets and the delta.

Why this script exists
----------------------
ADR-016 set the trigger line for rerank: "先有度量再谈精排，无 gold set 调精排
是盲调". This script is that measurement. It does **not** claim rerank helps; it
prints the delta and lets the reader decide (ADR-015: no claim without a number).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.config  # noqa: F401  (side effect: load_dotenv())
from app.vectordb import VectorDocument, VectorDBClient
from app.vectordb.rerank import DeterministicLexicalReranker
from evals.run_evals import _build_store_retrieve_fn, load_corpus_chunks, run_retrieval_eval

GOLD_PATH = ROOT / "evals" / "datasets" / "retrieval_gold.jsonl"

_METRIC_KEYS = ("hit_at_1", "hit_at_3", "hit_at_5", "recall_at_5", "mrr", "ndcg_at_5")


async def _run() -> dict[str, Any]:
    if not GOLD_PATH.exists():
        raise SystemExit(f"gold set not found: {GOLD_PATH}")
    cases = [
        json.loads(line)
        for line in GOLD_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    store = VectorDBClient()
    for doc_id, idx, text in load_corpus_chunks():
        await store.upsert(
            VectorDocument(
                id=f"{doc_id}:chunk:{idx}",
                content=text,
                metadata={"source": "knowledge", "doc_id": doc_id, "chunk": idx},
            )
        )

    base = await run_retrieval_eval(cases, retrieve_fn=_build_store_retrieve_fn(store))

    reranker = DeterministicLexicalReranker()

    async def _retrieve_with_rerank(query: str, top_k: int) -> list[str]:
        result = await store.search(query, top_k=max(top_k * 4, 20))
        docs = list(result.documents)
        docs = list(await reranker.rerank(query, docs, top_k))
        return [doc.id for doc in docs]

    reranked = await run_retrieval_eval(cases, retrieve_fn=_retrieve_with_rerank)

    delta = {
        key: round(reranked[key] - base[key], 4)
        for key in _METRIC_KEYS
    }
    return {"base": base, "rerank": reranked, "delta": delta}


def main() -> int:
    report = asyncio.run(_run())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
