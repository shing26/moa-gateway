"""Build the retrieval gold set with an independent annotator.

    uv run python scripts/label_retrieval_gold.py --mode llm
    uv run python scripts/label_retrieval_gold.py --mode heuristic
    uv run python scripts/label_retrieval_gold.py --finalize

Why this script exists
----------------------
ADR-020 requires that the gold set annotator is not the chunker author, otherwise
"RRF is better than the weighted sum" is unfalsifiable: the person who decided how
to split documents also decided what is relevant, so the metric measures their own
mental model, not retrieval quality. This script makes the annotator an explicit,
recorded choice instead of an implicit one.

Two annotator modes
-------------------
* ``llm``      - asks the configured LLM to propose queries and relevant chunk
                  ids per document. The annotator is a *different model* than any
                  human, which is the independence ADR-020 asks for. Requires
                  ``LLM_API_KEY`` (or ``OPENAI_API_KEY``); falls back to heuristic
                  with a warning when absent.
* ``heuristic`` - deterministic phrase-extraction fallback. It does **not** reuse
                  the chunker's logic for relevance (relevance is phrase presence in
                  the chunk text, not a chunking decision), but it is still written
                  by the same author as the chunker, so the resulting gold set is
                  only provisional until a human spot-checks it. The mode is recorded
                  in ``retrieval_gold.meta.json`` so the caveat travels with the data.

``--finalize`` applies human corrections from a review file and writes the final
``retrieval_gold.jsonl``. Until that step the report only prints numbers and must
not claim "retrieval improved" (ADR-015: no claim without a number, and no number
whose annotator is unaccounted for).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 显式加载 .env.local（而不是 .env）：本脚本是 LLM annotator，需要 Ollama 配置；
# 但 .env 会被 app.config 的 load_dotenv() 自动加载，导致 offline eval 也读到
# LLM_MODEL → CoderAgent/GeneralAgent/ReviewAgent 走真实 LLM 路径 → litellm 挂起
# （Ollama 没运行时）。.env.local 是 annotator 专用配置，offline eval 不加载它。
from dotenv import load_dotenv

load_dotenv(ROOT / ".env.local")

# app.config 在 import 期调用 load_dotenv()；不先 import 它，LLMConfig.from_env
# 读不到 .env.local 里的 LLM_API_KEY，会静默退化成"未配置"。
import app.config  # noqa: F401  (side effect: load_dotenv())

from app.knowledge import CHUNK_OVERLAP, CHUNK_SIZE, chunk_text  # noqa: E402

CORPUS_DIR = ROOT / "evals" / "datasets" / "retrieval_corpus"
GOLD_PATH = ROOT / "evals" / "datasets" / "retrieval_gold.jsonl"
STAGING_PATH = ROOT / "evals" / "datasets" / "retrieval_gold.staging.jsonl"
META_PATH = ROOT / "evals" / "datasets" / "retrieval_gold.meta.json"
REVIEW_PATH = ROOT / "evals" / "datasets" / "retrieval_gold.review.json"


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    index: int
    text: str

    @property
    def chunk_id(self) -> str:
        return f"{self.doc_id}:chunk:{self.index}"


def _doc_id(path: Path) -> str:
    return path.stem


def load_corpus() -> list[tuple[str, str]]:
    if not CORPUS_DIR.exists():
        raise SystemExit(f"corpus dir not found: {CORPUS_DIR}")
    docs: list[tuple[str, str]] = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        docs.append((_doc_id(path), path.read_text(encoding="utf-8")))
    return docs


def chunk_corpus(docs: list[tuple[str, str]]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc_id, text in docs:
        for i, piece in enumerate(chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)):
            chunks.append(Chunk(doc_id=doc_id, index=i, text=piece))
    return chunks


# ── heuristic annotator ─────────────────────────────────────────────────

_STOP = frozenset(
    "的 了 是 在 与 和 或 及 等 有 一 不 也 都 就 而 且 但 对 为 以 于 之 其 这 那 你 我 他 她 它 们 上 下 中 个 些 什么 如何 怎么 请问 一个 以及 可以 进行 通过 使用 需要 时候 因为 所以 如果 虽然 已经 还是 或者 并且 然后 接着 再 更 最 非常 比较 相对 关于 对于 由于 根据 按照 随着 为了 作为 被 把 让 向 从 到 给 用 按 比 很 太 真 好 大 小 多 少 高 低 长 短 新 旧 主 次 正 反 同 异 内 外 前 后 左 右 东 西 南 北".split()
)


def _distinctive_phrases(text: str, *, limit: int = 6) -> list[str]:
    """Pick CJK/ASCII phrases that are unlikely to be stop words.

    This is deliberately a *different* signal from the chunker: the chunker decides
    where boundaries fall, this picks which phrases are distinctive enough to be a
    query. Relevance is then phrase presence, not a chunking decision.
    """
    candidates: list[str] = []
    for match in re.finditer(r"[A-Za-z0-9_.\-/]{3,}", text):
        token = match.group(0).strip("._-/")
        if len(token) >= 3 and token.lower() not in _STOP:
            candidates.append(token)
    for match in re.finditer(r"[\u4e00-\u9fff]{4,8}", text):
        phrase = match.group(0)
        if phrase not in _STOP:
            candidates.append(phrase)
    # Prefer longer phrases (more specific), dedupe, keep order.
    ordered = sorted(dict.fromkeys(candidates), key=lambda s: (-len(s), s))
    return ordered[:limit]


def _heuristic_labels(chunks: list[Chunk], docs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for doc_id, text in docs:
        doc_chunks = [c for c in chunks if c.doc_id == doc_id]
        if not doc_chunks:
            continue
        for phrase in _distinctive_phrases(text):
            relevant = [c for c in doc_chunks if phrase in c.text]
            if not relevant:
                continue
            graded = {c.chunk_id: (2 if c is relevant[0] else 1) for c in relevant[:2]}
            rows.append(
                {
                    "id": f"q{len(rows) + 1:03d}",
                    "query": phrase,
                    "relevant_doc_ids": [doc_id],
                    "relevant_chunk_ids": list(graded),
                    "graded": graded,
                    "notes": "heuristic phrase extraction; spot-check required",
                }
            )
    return rows



# ── LLM annotator ───────────────────────────────────────────────────────

_LABEL_SYSTEM = (
    "You are labeling a retrieval gold set. For each document, propose 2-4 realistic "
    "user queries that this document can answer, and mark which chunk indices are "
    "relevant to each query. Be strict: a chunk is relevant only if it actually "
    "contains the answer. "
    "IMPORTANT: (1) propose queries answerable by LATER chunks too, not only the "
    "intro chunk; (2) when a query needs information from several chunks, mark all of "
    "them, grading the best chunk 2 and supporting chunks 1; (3) include a few queries "
    "whose answer lives in a chunk that does NOT contain the most obvious keyword. "
    "Output JSON only: "
    '{"queries": [{"query": "...", "chunk_indices": [0, 1], "graded": {"0": 2, "1": 1}}]}'
)


async def _llm_labels(chunks: list[Chunk], docs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    from app.agents.provider import LLMClient, LLMConfig

    config = LLMConfig.from_env("LLM")
    if not config.api_key:
        raise RuntimeError("LLM_API_KEY not configured; use --mode heuristic for a provisional gold set")
    rows: list[dict[str, Any]] = []
    async with LLMClient(config) as client:
        for doc_id, text in docs:
            doc_chunks = [c for c in chunks if c.doc_id == doc_id]
            if not doc_chunks:
                continue
            listing = "\n".join(
                f"[{c.index}] {c.text[:400]}" for c in doc_chunks
            )
            prompt = (
                f"Document id: {doc_id}\nChunks:\n{listing}\n\n"
                "Propose 3-4 queries a user might ask that this document answers. "
                "Cover later chunks, not only the intro. When a query spans several "
                "chunks, mark them all (best=2, supporting=1). Return JSON with a "
                "'queries' array."
            )
            raw = await client.chat(
                [
                    {"role": "system", "content": _LABEL_SYSTEM},
                    {"role": "user", "content": prompt},
                ]
            )
            parsed = _parse_json_object(raw)
            for item in parsed.get("queries", []) or []:
                query = str(item.get("query", "")).strip()
                if not query:
                    continue
                indices = [int(i) for i in item.get("chunk_indices", []) if str(i).lstrip("-").isdigit()]
                graded_raw = item.get("graded") or {}
                graded: dict[str, int] = {}
                for idx in indices:
                    if 0 <= idx < len(doc_chunks):
                        graded[doc_chunks[idx].chunk_id] = int(graded_raw.get(str(idx), 1) or 1)
                if not graded:
                    continue
                rows.append(
                    {
                        "id": f"q{len(rows) + 1:03d}",
                        "query": query,
                        "relevant_doc_ids": [doc_id],
                        "relevant_chunk_ids": list(graded),
                        "graded": graded,
                        "notes": "llm annotator; spot-check required",
                    }
                )
    return rows


def _parse_json_object(raw: str) -> dict[str, Any]:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if "```" in cleaned:
            cleaned = cleaned.split("```")[0]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# ── finalize ────────────────────────────────────────────────────────────

def _write_meta(annotator: str, *, finalized: bool, row_count: int) -> None:
    META_PATH.write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "annotator": annotator,
                "annotator_mode": annotator,
                "spot_check_required": not finalized,
                "spot_check_completed": finalized,
                "row_count": row_count,
                "corpus_dir": str(CORPUS_DIR.relative_to(ROOT)),
                "chunk_size": CHUNK_SIZE,
                "chunk_overlap": CHUNK_OVERLAP,
                "note": (
                    "annotator != chunker author (ADR-020). "
                    "heuristic mode is provisional until a human spot-check passes --finalize."
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _finalize() -> None:
    if not STAGING_PATH.exists():
        raise SystemExit(f"staging gold set not found: {STAGING_PATH}")
    corrections: dict[str, Any] = {}
    if REVIEW_PATH.exists():
        corrections = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in STAGING_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    keep = corrections.get("keep", [])
    drop = set(corrections.get("drop", []))
    graded_overrides = corrections.get("graded", {})
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("id") in drop:
            continue
        if graded_overrides.get(row["id"]):
            row["graded"] = graded_overrides[row["id"]]
        row["relevant_chunk_ids"] = list(row["graded"])
        row["notes"] = "human spot-checked"
        out.append(row)
    # keep only rows that survive the human review; if no review file exists the
    # set is left untouched and flagged as unspot-checked.
    if REVIEW_PATH.exists():
        GOLD_PATH.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out),
            encoding="utf-8",
        )
        _write_meta(
            corrections.get("annotator", "human-spot-check"),
            finalized=True,
            row_count=len(out),
        )
        print(f"wrote {len(out)} finalized rows -> {GOLD_PATH}")
    else:
        print(
            f"{len(out)} rows still in staging; write {REVIEW_PATH} "
            "{\"keep\": [...], \"drop\": [...], \"graded\": {...}} and re-run --finalize",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["llm", "heuristic"],
        default=None,
        help="annotator; defaults to llm when LLM_API_KEY is set, else heuristic",
    )
    parser.add_argument("--finalize", action="store_true", help="apply human review and write the final gold set")
    args = parser.parse_args(argv)

    if args.finalize:
        _finalize()
        return 0

    docs = load_corpus()
    chunks = chunk_corpus(docs)
    mode = args.mode
    if mode is None:
        mode = "llm" if (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")) else "heuristic"

    if mode == "llm":
        try:
            rows = asyncio.run(_llm_labels(chunks, docs))
        except Exception as exc:  # noqa: BLE001 - fall back rather than lose the run
            print(f"llm annotator unavailable ({exc}); falling back to heuristic", file=sys.stderr)
            mode = "heuristic"
            rows = _heuristic_labels(chunks, docs)
    else:
        rows = _heuristic_labels(chunks, docs)

    STAGING_PATH.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    _write_meta(mode, finalized=False, row_count=len(rows))
    print(f"mode={mode} rows={len(rows)} -> {STAGING_PATH}")
    print(f"meta -> {META_PATH}")
    print(
        "Next: spot-check the staging file, write retrieval_gold.review.json, "
        "then re-run with --finalize.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
