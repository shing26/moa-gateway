"""把一次检索「摊开」在终端上：精排开/关两栏并排看。

    uv run python scripts/rag_case.py                       # 默认案例
    uv run python scripts/rag_case.py "混合检索的两条腿怎么融合排序？"
    uv run python scripts/rag_case.py --reranker lexical    # 默认，纯本地

为什么要有这个脚本
----------------
``Hit@3 从 0.72 到 0.88`` 是一句没有画面的话。真正能说明问题的是**同一个 query
在开/关精排下，Top-3 分别捞回了什么**——尤其是"对的 chunk 被顶下去了"和
"模型的上下文里混进了错的 chunk"这两种情况，只有摊开才看得见。

因此本脚本**不预设结论**。它把两栏结果和 gold 标注一起打出来，delta 是正是负
都由数据说话。默认案例就是一条**变差了**的：query「请求链路是如何工作的？」下，
``01-gateway-overview:chunk:0``（gold 分级 2）本就在第一位，开精排后被
``chunk:1``（分级 1）顶到第二。两条都还在 Top-3 里，所以 Hit@3 看不出来，
但 nDCG 会掉——这正是离线六项 delta 全负其中一项的来源。挑选默认案例的标准
是"差异看得见"，不是"结果好看"。

语料与 gold set 跟离线评测**同一份**（``evals/run_evals.py`` 的
``load_corpus_chunks``），所以这里看到的分块 id 与真实写入知识库的一致。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from termui import kv, paint, rule, width, wrap  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_QUERY = "请求链路是如何工作的？"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="并排对比一次检索的精排开/关")
    parser.add_argument("query", nargs="?", default=DEFAULT_QUERY, help="要检索的 query")
    parser.add_argument("--top-k", type=int, default=3, help="展示前几条（默认 3）")
    parser.add_argument(
        "--reranker",
        choices=["lexical", "none"],
        default="lexical",
        help="右侧用哪种精排器；none 表示两侧都是纯混合检索（自检用）",
    )
    return parser.parse_args(argv)


async def _build_store():
    from evals.run_evals import load_corpus_chunks
    from app.vectordb import VectorDBClient, VectorDocument

    store = VectorDBClient()
    for doc_id, idx, text in load_corpus_chunks():
        await store.upsert(
            VectorDocument(
                id=f"{doc_id}:chunk:{idx}",
                content=text,
                metadata={"source": "knowledge", "doc_id": doc_id, "chunk": idx},
            )
        )
    return store


def _gold_for(query: str) -> set[str]:
    """从 gold set 里取这个 query 的相关 chunk；不在 gold set 里就返回空集。"""
    gold_path = ROOT / "evals" / "datasets" / "retrieval_gold.jsonl"
    if not gold_path.exists():
        return set()
    for line in gold_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if str(row.get("query", "")).strip() == query.strip():
            return set(row.get("relevant_chunk_ids") or [])
    return set()


def _snippet(text: str, limit: int = 110) -> str:
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _column(docs, top_k: int, gold: set[str], title: str, colour: str) -> list[str]:
    lines = [paint(f"  {title}", "bold", colour)]
    if not docs:
        lines.append(paint("    （无结果）", "dim"))
        return lines
    for rank, doc in enumerate(docs[:top_k], start=1):
        mark = paint(" ◆gold", "yellow") if doc.id in gold else ""
        lines.append(
            paint(f"    {rank}. ", "dim")
            + paint(doc.id, "cyan")
            + mark
            + paint(f"  score={doc.score:.4f}", "dim")
        )
        lines.append(wrap(_snippet(doc.content), indent="        ", initial="        "))
    hits = sum(1 for doc in docs[:top_k] if doc.id in gold)
    lines.append(
        paint(f"    → Top-{top_k} 命中 {hits}/{len(gold)} 条 gold", "dim")
    )
    return lines


def _side_by_side(left: list[str], right: list[str]) -> str:
    """两栏并排；终端太窄时退化成上下堆叠。"""
    total = width()
    if total < 110:
        return "\n".join(left + [""] + right)
    gap = 4
    col = max(30, (total - gap) // 2)
    out: list[str] = []
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else ""
        b = right[index] if index < len(right) else ""
        out.append(f"{a:<{col}}{' ' * gap}{b}")
    return "\n".join(out)


async def _run(args: argparse.Namespace) -> int:
    from app.vectordb.rerank import DeterministicLexicalReranker

    store = await _build_store()
    gold = _gold_for(args.query)

    if args.reranker == "none":
        base = await store.search(args.query, top_k=args.top_k)
        print(rule("检索对照 · 两侧均为纯混合检索（自检，delta 应全为 0）"))
        print(kv("query", args.query))
        print(kv("gold", f"{len(gold)} 条" + ("" if gold else "（该 query 不在 gold set 中）")))
        print()
        print("\n".join(_column(list(base.documents), args.top_k, gold, "混合检索（RRF）", "cyan")))
        return 0

    # 关精排：直接按 RRF 顺序截断
    off_result = await store.search(args.query, top_k=args.top_k)
    off = list(off_result.documents)

    # 开精排：按 ContextRetriever 的做法取宽候选 → 精排 → 截到 top_k
    candidate_k = max(args.top_k * 4, 20)
    wide = await store.search(args.query, top_k=candidate_k)
    reranker = DeterministicLexicalReranker()
    on = list(await reranker.rerank(args.query, list(wide.documents), args.top_k))[
        : args.top_k
    ]

    print(rule("检索对照 · 精排关 vs 开"))
    print(kv("query", args.query))
    print(kv("corpus", "固定语料 6 篇 12 chunk（内存后端，真实分词 + RRF）"))
    print(kv("gold", f"{len(gold)} 条" + ("" if gold else "（该 query 不在 gold set 中）")))
    print()
    print(
        _side_by_side(
            _column(off, args.top_k, gold, "关精排（纯 RRF 顺序）", "cyan"),
            _column(on, args.top_k, gold, "开精排（宽候选 + Lexical）", "magenta"),
        )
    )
    print()

    def hit1(docs) -> bool:
        return bool(docs) and docs[0].id in gold

    off_hit, on_hit = hit1(off), hit1(on)
    if off_hit and not on_hit:
        verdict = paint("开精排把第一名的正确 chunk 挤掉了", "bold", "red")
    elif on_hit and not off_hit:
        verdict = paint("开精排把正确的 chunk 捞到了第一位", "bold", "green")
    elif not gold:
        verdict = paint("该 query 不在 gold set 中，只看排序变化，不判对错", "dim")
    else:
        verdict = paint("两侧 Top-1 对错一致", "dim")
    print(kv("Top-1", f"{'对' if off_hit else '错'} → {'对' if on_hit else '错'}"))
    print(kv("结论", verdict))
    if gold and off_hit and on_hit and off[0].id == on[0].id:
        print(paint("  两侧首位是同一条 chunk，本次没有可观察的差异。", "dim"))
    elif gold and off_hit == on_hit and off != on:
        print(
            paint("  Top-1 对错没变，但 **排序变了**：见上表同名 chunk 的位置。", "yellow")
            + paint("  Hit@k 对此不敏感，nDCG 会。", "dim")
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
