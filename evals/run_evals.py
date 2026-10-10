from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.guard.guard_service import guard_service
from app.models.events import MoAEvent
from app.pipeline import PipelineResult
from app.router.intent_router import IntentRouter

logger = logging.getLogger("moa.evals.runner")


def load_dataset(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


# 一致性维度的重放次数。5 是折中：少于 3 次看不出摆动；每次都是一次真实 LLM
# 调用，再多对本地小模型太慢。
CONSISTENCY_REPEATS = 5


async def run_intent_eval(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """意图路由准确率。

    **只测正则表**：这里构造的 ``IntentRouter()`` 不注入微模型/路由 LLM，所以
    正则未命中的用例只会拿到 ``default_intent``。数据集里那几条非正则用例的
    ``expected_intent`` 恰好就是默认值 ``assistant``，因此本维度的 1.0 是"正则表
    与标签集出自同一心智模型"的结果，不含模型路径的信息。模型兜底路径的稳定性
    由 ``run_intent_consistency_eval`` 单独测。
    """
    router = IntentRouter()
    correct = 0
    confusion: dict[str, dict[str, int]] = {}
    for case in cases:
        actual, _ = await router.route(str(case.get("input", "")))
        expected = str(case.get("expected_intent", ""))
        confusion.setdefault(expected, {})
        confusion[expected][actual] = confusion[expected].get(actual, 0) + 1
        if actual == expected:
            correct += 1
    total = len(cases)
    return {
        "total": total,
        "correct": correct,
        "accuracy": _ratio(correct, total),
        "confusion": confusion,
    }


async def run_intent_consistency_eval(
    cases: list[dict[str, Any]],
    *,
    repeats: int = CONSISTENCY_REPEATS,
    router: Any = None,
) -> dict[str, Any]:
    """同一输入重放 N 次，测意图判定的**一致率**。

    与 ``run_intent_eval`` 的根本区别：那个维度比对"是否等于期望意图"，数据集、
    正则表、标签出自同一设计者，所以恒 1.0 不含信息。**本维度没有任何期望值**，
    只看同一输入多次判定是否收敛——因此设计者无法自证：除非系统真的稳定，写不出
    1.0。它是对"这套评测有没有区分度"这个问题的直接回答。

    它测的是**模型兜底路径**，所以必须真注入 LLM（默认取 ``app.deps.router``）；
    数据集用例必须不命中任何意图正则，由
    ``test_intent_consistency_cases_bypass_the_regex_table`` 钉住。

    **必须一起看 ``levels`` / ``degraded_calls``**：路由对每个请求返回
    ``(intent, level)``，``level="none"`` 表示微模型/路由 LLM 那一层没给出判定
    （未配置、超时或报错），于是 intent 是**默认值**。此时"稳定"是"一致地降级"，
    不是"判断稳定"——2026-09-22 实测正是这种情形（冷启动首次调用 4.2s 超过
    ``ROUTER_LLM_TIMEOUT_MS=2000``，超时取消后模型热不起来，55/55 全是 none）。
    所以稳定率 1.0 单独拿出来看毫无意义，报告必须把降级次数放在旁边。

    **故意不设数值门禁**：在拿到真实数值之前定一个阈值就是编数字，而"编数字"正是
    这个维度要治的病。报告只给数，不稳定用例逐个列出。
    """
    if router is None:
        from app.deps import router as default_router

        router = default_router
    repeats = max(2, int(repeats))

    stable = 0
    agreement_sum = 0.0
    unstable: list[dict[str, Any]] = []
    levels: dict[str, int] = {}
    for case in cases:
        text = str(case.get("input", ""))
        seen: list[str] = []
        for _ in range(repeats):
            intent, level = await router.route(text)
            seen.append(str(intent))
            levels[str(level)] = levels.get(str(level), 0) + 1
        counts = Counter(seen)
        top = max(counts.values())
        agreement_sum += top / repeats
        if top == repeats:
            stable += 1
        else:
            unstable.append({
                "id": case.get("id", ""),
                "input": text,
                "intents": dict(counts),
            })

    total = len(cases)
    total_calls = total * repeats
    degraded = int(levels.get("none", 0))
    if total_calls and degraded == total_calls:
        note = "全部调用降级到默认意图（路由 LLM 未配置或超时）——稳定率不代表判断质量"
    elif degraded:
        note = f"{degraded}/{total_calls} 次调用降级到默认意图，稳定率被高估"
    else:
        note = ""
    return {
        "total": total,
        "repeats": repeats,
        "stable": stable,
        "stable_rate": _ratio(stable, total),
        "avg_agreement": round(agreement_sum / total, 4) if total else 0.0,
        "unstable": unstable,
        "levels": levels,
        "degraded_calls": degraded,
        "note": note,
        "skipped": 0,
    }


def _consistency_skipped(
    cases: list[dict[str, Any]], *, repeats: int = CONSISTENCY_REPEATS
) -> dict[str, Any]:
    """--offline 的形态。

    纯正则表是确定性的，拿它测一致率必然 1.0 —— 那是**假的一致**，比不测更糟
    （会让人以为模型路径也稳）。所以离线如实标 skipped，而不是给一个漂亮的 1.0。
    """
    return {
        "total": len(cases),
        "repeats": repeats,
        "stable": 0,
        "stable_rate": 0.0,
        "avg_agreement": 0.0,
        "unstable": [],
        "levels": {},
        "degraded_calls": 0,
        "note": "",
        "skipped": len(cases),
    }


async def run_tool_selection_eval(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """工具选择准确率：离线确定性，不依赖任何 LLM 或网络。

    被测对象是"选哪个工具"这一步——规则命中、参数装配、工具注册名一致。
    这是 Agent 级指标里唯一能在 CI 里稳定复现的一项（其余依赖真实流量）。
    """
    from app.agent_core.mock_llm import MockTaskLLM

    llm = MockTaskLLM()
    correct = 0
    misses: list[dict[str, str]] = []
    for case in cases:
        text = str(case.get("input", ""))
        decision = await llm.decide(task=text, subtask=text, observations=[])
        predicted = decision.tool_name if decision.action == "call_tool" else ""
        expected = str(case.get("expected_tool", ""))
        if predicted == expected:
            correct += 1
        else:
            misses.append({"input": text, "expected": expected, "predicted": predicted})
    return {
        "total": len(cases),
        "correct": correct,
        "accuracy": _ratio(correct, len(cases)),
        "misses": misses,
    }


async def run_guard_eval(cases: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    review_tp = review_fp = review_fn = 0
    correct = 0
    for case in cases:
        expected = str(case.get("expected_action", ""))
        verdict, _ = guard_service.evaluate_output(
            str(case.get("input", "")),
            intent="assistant",
            hitl_enabled=False,
        )
        actual = verdict.action.value
        if expected == actual:
            correct += 1
        if expected == "deny" and actual == "deny":
            tp += 1
        elif expected == "deny" and actual != "deny":
            fn += 1
        elif expected != "deny" and actual == "deny":
            fp += 1
        else:
            tn += 1
        if expected == "review" and actual == "review":
            review_tp += 1
        elif expected == "review" and actual != "review":
            review_fn += 1
        elif expected != "review" and actual == "review":
            review_fp += 1
    return {
        "total": len(cases),
        "correct": correct,
        "accuracy": _ratio(correct, len(cases)),
        "deny_recall": _ratio(tp, tp + fn),
        "deny_precision": _ratio(tp, tp + fp),
        "false_positive": fp,
        "deny_positive": tp + fn,
        "deny_negative": fp + tn,
        "review_recall": _ratio(review_tp, review_tp + review_fn),
        "review_precision": _ratio(review_tp, review_tp + review_fp),
        "mislabeled": len(cases) - correct,
    }


# ── 检索质量评测（ADR-020 验收 3）─────────────────────────────────────────

_RETRIEVAL_CORPUS_DIR = ROOT / "evals" / "datasets" / "retrieval_corpus"


def load_corpus_chunks() -> list[tuple[str, int, str]]:
    """把固定语料切成 (doc_id, chunk_index, text)。

    分块器与知识库写入用的是同一个 ``chunk_text``，因此 gold set 的 chunk id
    （``{doc_id}:chunk:{i}``）与检索时实际写入的 id 一致——这是"gold set 能
    被检索到"的前提，不是巧合。
    """
    from app.knowledge import CHUNK_OVERLAP, CHUNK_SIZE, chunk_text

    if not _RETRIEVAL_CORPUS_DIR.exists():
        return []
    out: list[tuple[str, int, str]] = []
    for path in sorted(_RETRIEVAL_CORPUS_DIR.glob("*.md")):
        doc_id = path.stem
        text = path.read_text(encoding="utf-8")
        for i, piece in enumerate(chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)):
            out.append((doc_id, i, piece))
    return out


def _dcg(graded: dict[str, int], ranked_ids: list[str], k: int) -> float:
    """Discounted cumulative gain@k，按 gold set 的相关度分级。"""
    import math

    total = 0.0
    for rank, doc_id in enumerate(ranked_ids[:k], start=1):
        rel = graded.get(doc_id, 0)
        if rel:
            total += (2 ** rel - 1) / math.log2(rank + 1)
    return total


async def run_retrieval_eval(
    cases: list[dict[str, Any]],
    *,
    retrieve_fn: Any,
    top_k: int = 5,
) -> dict[str, Any]:
    """检索质量：Hit@k / Recall@k / MRR / nDCG@k。

    ``retrieve_fn(query, top_k) -> list[str]`` 返回 chunk id 列表（已排序）。
    调用方决定它背后是纯 store、还是 store + reranker——本函数只负责度量，
    因此 base 与 rerank 两条路径可以共用同一把尺子做前后对比。

    **不判分、不宣称更准**：这里只出数字。"rerank 是否有用"由调用方在 gold set
    上跑两条路径后自己读 delta，本函数不替它下结论（ADR-015 口径）。
    """
    if not cases:
        return _retrieval_skipped("gold set 为空")

    hit = {1: 0, 3: 0, 5: 0}
    recall_sum = 0.0
    mrr_sum = 0.0
    ndcg_sum = 0.0
    per_case: list[dict[str, Any]] = []
    evaluated = 0
    for case in cases:
        query = str(case.get("query", ""))
        relevant = set(case.get("relevant_chunk_ids", []) or [])
        graded = case.get("graded") or {}
        if not relevant:
            # 没有相关 chunk 的行无法评分。分母必须用**实际评过的行数**：
            # 用 len(cases) 的话，一条坏行会把所有指标静默稀释成 (n-1)/n，
            # 而报告里看不出少评了一条。
            continue
        evaluated += 1
        ranked = list(await retrieve_fn(query, top_k))
        topk = ranked[:top_k]
        for k in hit:
            if relevant & set(topk[:k]):
                hit[k] += 1
        recall_sum += len(relevant & set(topk)) / len(relevant)
        first_rank = next(
            (i for i, doc_id in enumerate(topk, start=1) if doc_id in relevant),
            0,
        )
        if first_rank:
            mrr_sum += 1.0 / first_rank
        ideal = [doc_id for doc_id, _ in sorted(graded.items(), key=lambda kv: -kv[1])]
        idcg = _dcg(graded, ideal, top_k)
        ndcg_sum += (_dcg(graded, topk, top_k) / idcg) if idcg else 0.0
        per_case.append(
            {
                "id": case.get("id", ""),
                "query": query[:60],
                "first_relevant_rank": first_rank or None,
                "retrieved": topk[:5],
            }
        )
    total = len(cases)
    skipped = total - evaluated
    return {
        "total": total,
        "evaluated": evaluated,
        "hit_at_1": _ratio(hit[1], evaluated),
        "hit_at_3": _ratio(hit[3], evaluated),
        "hit_at_5": _ratio(hit[5], evaluated),
        "recall_at_5": round(recall_sum / evaluated, 4) if evaluated else 0.0,
        "mrr": round(mrr_sum / evaluated, 4) if evaluated else 0.0,
        "ndcg_at_5": round(ndcg_sum / evaluated, 4) if evaluated else 0.0,
        "per_case": per_case,
        # 有行被跳过必须说出来，否则"少评了一条"在报告里不可见
        "note": (
            f"{skipped} 行缺少 relevant_chunk_ids，未参与评分" if skipped else ""
        ),
    }


def _retrieval_skipped(reason: str) -> dict[str, Any]:
    return {
        "total": 0,
        "evaluated": 0,
        "hit_at_1": 0.0,
        "hit_at_3": 0.0,
        "hit_at_5": 0.0,
        "recall_at_5": 0.0,
        "mrr": 0.0,
        "ndcg_at_5": 0.0,
        "per_case": [],
        "note": reason,
    }


def _build_store_retrieve_fn(store: Any) -> Any:
    """retrieve_fn：包装任意 ``VectorStore.search``，零网络（离线用内存后端）。"""

    async def _retrieve(query: str, top_k: int) -> list[str]:
        result = await store.search(query, top_k=top_k)
        return [doc.id for doc in result.documents]

    return _retrieve


# 显式"离线不判分"的哨兵。用哨兵而不是 `judge=None`，是因为 None 已经被
# `judge or default_judge` 用作"用默认 judge"的意思，两者必须分得开。
_SKIP_JUDGE = object()


class _JudgeSkipped(Exception):
    """离线路径没有判分器。

    用它走 `run_e2e_eval` 里**已有的** except 分支，比给那段判分代码再加一层缩进
    稳妥得多——同时保证"没读数"（离线）与"判分失败"（provider 挂了）分开计数。
    """


async def _no_judge(*_args: Any, **_kwargs: Any) -> float:
    """离线 e2e 的"判分器"：不判分，也**不假装有分数**——0 分是"答案差"。"""
    raise _JudgeSkipped()


async def run_e2e_offline(
    cases: list[dict[str, Any]],
    pipeline: Any | None = None,
) -> dict[str, Any]:
    """离线 e2e：**真跑业务链路**，零网络、零 token。

    （2026-09-28 修）此前这里是一个连 pipeline 都不构造的桩，跑完把 30 条一律记成
    skipped——于是 CI 里那趟 "Eval smoke" **从未冒烟到业务链路**，"评测能跑"这件事
    一直没有机器背书（ADR-016 台账里判为"要修"，见该台账补记）。

    现在走 `app.deps.build_offline_pipeline()`：与线上同源的路由 → agent → 评估 →
    守卫 → HITL → 审计，只把路由换成纯正则、agent 后端交给 `AGENT_LLM`（CI 里即 `mock`）。

    **不跑 LLM judge**：判分需要模型，那是活体路径（`run_e2e_eval`）的事。这里把 judge
    如实标成"未跑"（`judge_skipped`），而不是记 0 分——0 分是"答案差"，没读数不该长得像
    0 分（与 ADR-016 的 `judge_failures` 同一条口径）。

    已知副作用并接受：会把 `eval-offline-` 前缀的条目写进审计日志。
    `scripts/collect_hitl_feedback.py` 已把 `eval` 前缀当合成流量排除，不污染真实指标。
    """
    if pipeline is None:
        from app.deps import build_offline_pipeline

        pipeline = build_offline_pipeline()
    result = await run_e2e_eval(
        cases,
        pipeline=pipeline,
        judge=_SKIP_JUDGE,
        use_store=False,
        case_prefix="eval-offline",
    )
    # 保留旧字段名，报告读者不必跟着改；语义从"30 个桩跑过"变成"30 条真跑过"。
    result["offline_smoke"] = result["run"]
    return result


async def run_e2e_eval(
    cases: list[dict[str, Any]],
    *,
    pipeline: Any | None = None,
    judge: Any | None = None,
    use_store: bool = True,
    case_prefix: str = "eval",
) -> dict[str, Any]:
    from app.deps import init_prompts, vector_client
    from app.deps import pipeline as default_pipeline
    from evals.judge import score as default_judge

    from app.fsm.state_machine import Event

    # 服务进程在 FastAPI lifespan 里做这两件事；eval 进程没有 lifespan，需手动补。
    # 漏掉 vector_client.start() 时检索腿永远空转：pgvector 的 _pool 为 None，search
    # 静默返回空结果（日志里是 `后端已降级…（None）`），e2e 会"跑通"但测的是**没有
    # RAG 上下文**的链路——那正是本项要验的东西（2026-09-22 实测）。
    init_prompts()
    # 真实存储的启停必须显式，**不能**从 ``pipeline is None`` 推断：``--engine fsm``
    # 传入的 fsm_pipeline 是真实 runner，却会被那句推断判成"注入了假 pipeline"而跳过
    # start() → 同样是静默无 RAG 的链路（2026-09-22 实测 --engine 跑时日志刷
    # `后端已降级，search 返回空结果（None）`）。注入测试替身的调用方显式传
    # use_store=False，既不连库也不受本机 DSN 影响。
    if use_store:
        await vector_client.start()
    runner = pipeline or default_pipeline
    # judge=_SKIP_JUDGE（离线路径）表示**不判分**；judge=None 才是"用默认 judge"。
    judge_fn = _no_judge if judge is _SKIP_JUDGE else (judge or default_judge)
    scores: list[float] = []
    latencies: list[float] = []
    costs: list[float] = []
    status_matches = 0
    judge_failures = 0
    judge_skipped = 0
    intent_compared = 0
    intent_matches = 0
    intent_mismatches: list[dict[str, Any]] = []
    try:
        for case in cases:
            event = MoAEvent(
                trace_id=f"{case_prefix}-{case.get('id', 'unknown')}",
                event=Event.MESSAGE_RECEIVED,
                session_id=f"{case_prefix}-{case.get('id', 'unknown')}",
                text=str(case.get("input", "")),
                context={},
            )
            start = time.monotonic()
            result = await runner.run(event, channel="eval", target="eval")
            latency_ms = (time.monotonic() - start) * 1000
            latencies.append(latency_ms)
            costs.append(float(getattr(result, "cost_usd", 0.0) or 0.0))
            expected = case.get("expected", {})
            if isinstance(expected, dict) and expected.get("status") != result.status:
                scores.append(0.0)
            else:
                status_matches += 1
                # 数据集里声明的 intent 接上比对。此前该字段被写进用例却**从不被读**，
                # 是装饰字段。2026-09-23 首次比对：预热后 14/30、冷启动 13/30 —— 说明
                # 这是**结构性偏离**而非环境噪声，成因有两类：① 正则表缺陷（"写一个 X
                # 示例"不命中 coding、`查询/查找` 抢走编码请求、`代码` 抢 analyze）；
                # ② 路由 LLM 的系统性偏向（"解释/介绍/推荐某概念"一律判 translate）。
                #
                # **它是"与数据集标签的偏离率"，不是"路由准确率"**：10/16 例的期望值是
                # `assistant`，而那个值其实是**默认桶（哨兵）**而非语义真值——"今天天气
                # 怎么样"判成 search 反而更合理。要让这个数变成质量指标，得先重写数据集
                # 标签（把"确实应为 assistant"与"没命中所以是默认值"分开），属独立议题。
                #
                # **独立指标，不折进 success_rate**：状态相符与意图相符是两类不同的问题，
                # 混成一个数就再也分不出是哪一类在退化。
                exp_intent = expected.get("intent") if isinstance(expected, dict) else None
                if exp_intent:
                    intent_compared += 1
                    if exp_intent == result.intent:
                        intent_matches += 1
                    elif len(intent_mismatches) < 10:
                        intent_mismatches.append({
                            "id": case.get("id", ""),
                            "input": str(case.get("input", ""))[:40],
                            "expected": exp_intent,
                            "actual": result.intent,
                        })
                try:
                    judge_score = await judge_fn(
                        str(case.get("input", "")),
                        result.text,
                        str(case.get("judge_criteria", "")),
                    )
                except _JudgeSkipped:
                    # 离线路径没判分器：这是"没读数"，与"判分失败"分开计，
                    # 更不能混进 avg_judge_score（那会变成一句假的 0 分）。
                    judge_skipped += 1
                except Exception as exc:
                    # judge 失败 ≠ 得 0 分：0 分是"模型输出差"，judge 挂了是"量具没读数"。
                    # 剔除出均分并单独计数——既不能让 provider 一死整个评测崩溃、报告都不写
                    # （2026-09-27 实测：Ollama 进程死亡时 judge 的裸 chat 让
                    # InternalServerError 一路穿透 CLI，agent 链路自己倒是优雅降级了），
                    # 也绝不静默计 0 污染指标。judge_failures 字段让"没读数"可见。
                    judge_failures += 1
                    logger.warning(
                        "judge failed for case %s: %s", case.get("id", "?"), exc,
                    )
                else:
                    scores.append(judge_score)
    finally:
        if use_store:
            await vector_client.close()
    return {
        "total": len(cases),
        "run": len(cases),
        "skipped": 0,
        "success_rate": _ratio(status_matches, len(cases)),
        "intent_compared": intent_compared,
        "intent_matches": intent_matches,
        "intent_match_rate": _ratio(intent_matches, intent_compared),
        "intent_mismatches": intent_mismatches,
        "avg_judge_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "judge_failures": judge_failures,
        # 离线路径不判分：如实记"没读数"，别让 avg_judge_score 的 0.0 被读成"答案差"。
        "judge_skipped": judge_skipped,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "avg_cost_usd": round(sum(costs) / len(costs), 6) if costs else 0.0,
    }


def git_sha() -> str:
    try:
        output = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return output.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def build_summary(report: dict[str, Any]) -> str:
    intent = report["intent"]
    guard = report["guard"]
    e2e = report["e2e"]
    tool = report.get("tool_selection", {})
    hitl = report.get("hitl_feedback", {})
    metrics = report.get("agent_metrics", {})
    consistency = report.get("intent_consistency", {})
    if consistency.get("skipped"):
        consistency_part = f"intent_consistency skipped={consistency['skipped']} (离线无模型)"
    else:
        consistency_part = (
            f"intent_consistency stable={consistency.get('stable_rate')} "
            f"({consistency.get('stable')}/{consistency.get('total')}"
            f"×{consistency.get('repeats')}, unstable={len(consistency.get('unstable', []))}, "
            f"degraded={consistency.get('degraded_calls', 0)})"
        )
        # 降级次数不为 0 时给个显眼标记：稳定率此时是被高估的
        if consistency.get("note"):
            consistency_part += f" ⚠️ {consistency['note']}"
    if hitl.get("available"):
        synthetic = int(hitl.get("synthetic_cases", 0) or 0)
        # 全为模拟时必须写在脸上：这些数来自本地模拟点击，不是真实用户被拦。
        tag = (
            " (全为模拟)"
            if synthetic and synthetic >= hitl["cases"]
            else f" (synthetic={synthetic})"
        )
        hitl_part = (
            f"hitl cases={hitl['cases']}{tag} approve_rate={hitl['approve_rate']} "
            f"介入率={hitl['human_intervention_rate']}"
        )
    else:
        hitl_part = "hitl cases=0 (未采集)"
    e2e_part = (
        f"e2e run={e2e['run']} skipped={e2e['skipped']} "
        f"success={metrics.get('task_success_rate')}"
    )
    # 意图命中率只在真比对过时才显示
    if e2e.get("intent_compared"):
        e2e_part += (
            f" intent_match={e2e.get('intent_match_rate')} "
            f"({e2e.get('intent_matches')}/{e2e.get('intent_compared')})"
        )
    # judge 挂了几条必须写在脸上：avg_judge_score 只算"读到了数"的用例，
    # 不写这个标记的话，"均分 0.8"会让人误以为 30 条全被评过。
    if e2e.get("judge_failures"):
        e2e_part += f" ⚠️ judge_failures={e2e['judge_failures']}"
    # 离线路径不判分。同样必须写在脸上，否则 avg_judge_score 的 0.0 会被读成"答案差"。
    if e2e.get("judge_skipped"):
        e2e_part += f" judge未跑={e2e['judge_skipped']}"
    retrieval = report.get("retrieval", {})
    if retrieval.get("total"):
        retrieval_part = (
            f"retrieval Hit@1={retrieval.get('hit_at_1')} "
            f"Hit@3={retrieval.get('hit_at_3')} Hit@5={retrieval.get('hit_at_5')} "
            f"Recall@5={retrieval.get('recall_at_5')} MRR={retrieval.get('mrr')} "
            f"nDCG@5={retrieval.get('ndcg_at_5')} ({retrieval.get('total')} 条)"
        )
    else:
        retrieval_part = f"retrieval {retrieval.get('note', 'skipped')}"
    return (
        f"intent accuracy={intent['accuracy']} ({intent['correct']}/{intent['total']}), "
        f"{consistency_part}, "
        f"guard deny recall={guard['deny_recall']} precision={guard['deny_precision']}, "
        f"tool_select acc={tool.get('accuracy')} ({tool.get('correct')}/{tool.get('total')}), "
        f"{e2e_part}, "
        f"{hitl_part}, "
        f"{retrieval_part}"
    )


# 与 scripts/collect_hitl_feedback.py 同一口径：合成流量（探针/评测/测试夹具）的
# 会话前缀。命中即视为"不是真实用户产生的决策"。
_SYNTHETIC_SESSION_PREFIXES = ("probe", "eval", "test", "dash-test")


def _is_synthetic_session(session_id: Any) -> bool:
    sid = str(session_id or "").lower()
    return any(sid.startswith(prefix) for prefix in _SYNTHETIC_SESSION_PREFIXES)


def load_hitl_feedback(datasets_dir: Path) -> dict[str, Any]:
    """人工决策回流用例（scripts/collect_hitl_feedback.py 从审计采集）。

    这是"人工介入率/放行率"等真实业务指标的来源；数据集不存在时如实报告
    不可用，而不是编造 0。

    ``synthetic_cases`` / ``real_cases`` 是**必须看**的两个字段：随包数据集当前
    全是本地模拟点击产生的种子（会话前缀 ``probe-*``），也就是说那几个比率目前
    在统计上不具意义。派生自 session_id 前缀（口径与采集器一致），不需要改数据集
    schema；报告与命令行摘要都会把"全为模拟"标出来。
    """
    dataset_path = datasets_dir / "hitl_feedback.jsonl"
    meta_path = datasets_dir / "hitl_feedback.meta.json"
    if not dataset_path.exists():
        return {"available": False, "cases": 0}
    cases = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    approved = sum(1 for c in cases if c.get("decision") == "approve")
    latencies = [c["decision_latency_ms"] for c in cases if c.get("decision_latency_ms")]
    synthetic = sum(1 for c in cases if _is_synthetic_session(c.get("session_id")))
    return {
        "available": True,
        "cases": len(cases),
        "synthetic_cases": synthetic,
        "real_cases": len(cases) - synthetic,
        "approve_count": approved,
        "reject_count": len(cases) - approved,
        "approve_rate": _ratio(approved, len(cases)),
        "avg_decision_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "human_intervention_rate": float(meta.get("human_intervention_rate", 0.0) or 0.0),
        "requests": int(meta.get("requests", 0) or 0),
        "guard_interceptions": int(meta.get("guard_interceptions", 0) or 0),
        "unmatched_decisions": int(meta.get("unmatched_decisions", 0) or 0),
        "note": str(meta.get("note", "")),
        "generated_at": str(meta.get("generated_at", "")),
    }


def build_agent_metrics(
    e2e: dict[str, Any],
    tool_selection: dict[str, Any],
    hitl: dict[str, Any],
) -> dict[str, Any]:
    """Agent 级指标汇总：任务成功率 / 成本 / 延迟 / 工具选择准确率 / 人工介入率。"""
    return {
        "task_success_rate": e2e.get("success_rate", 0.0),
        "avg_cost_usd": e2e.get("avg_cost_usd", 0.0),
        "avg_latency_ms": e2e.get("avg_latency_ms", 0.0),
        "tool_selection_accuracy": tool_selection.get("accuracy", 0.0),
        "human_intervention_rate": hitl.get("human_intervention_rate") if hitl.get("available") else None,
        "approve_rate": hitl.get("approve_rate") if hitl.get("available") else None,
        "human_decisions": hitl.get("cases", 0) if hitl.get("available") else 0,
    }


def resolve_engine(engine: str | None) -> Any | None:
    """Map ``--engine`` to an e2e runner.

    ``None`` keeps ``run_e2e_eval``'s default, which is whatever ``ENGINE``
    selected in ``app.deps``. Naming an engine explicitly overrides that, so the
    same dataset can be run against both runtimes.
    """
    if engine is None:
        return None
    if engine == "fsm":
        from app.deps import fsm_pipeline

        return fsm_pipeline
    if engine == "langgraph":
        from app.orchestration.graph import LangGraphOrchestrator

        return LangGraphOrchestrator.from_deps()
    raise ValueError(f"unknown engine: {engine}")


async def run_all(
    offline: bool,
    datasets_dir: Path,
    *,
    engine: str | None = None,
) -> dict[str, Any]:
    intent = await run_intent_eval(load_dataset(datasets_dir / "intent.jsonl"))
    guard = await run_guard_eval(load_dataset(datasets_dir / "guard_redteam.jsonl"))
    tool_selection = await run_tool_selection_eval(
        load_dataset(datasets_dir / "tool_selection.jsonl")
    )
    hitl_feedback = load_hitl_feedback(datasets_dir)
    e2e_cases = load_dataset(datasets_dir / "e2e.jsonl")
    consistency_cases = load_dataset(datasets_dir / "intent_consistency.jsonl")
    consistency = (
        _consistency_skipped(consistency_cases)
        if offline
        else await run_intent_consistency_eval(consistency_cases)
    )
    e2e = (
        await run_e2e_offline(e2e_cases)
        if offline
        else await run_e2e_eval(e2e_cases, pipeline=resolve_engine(engine))
    )
    # 检索质量（ADR-020 验收 3）：离线用内存后端的真实词法检索，活体用真 pgvector。
    # gold set 不存在时如实标 skipped，而不是编 0。
    gold_path = datasets_dir / "retrieval_gold.jsonl"
    if not gold_path.exists():
        retrieval = _retrieval_skipped("retrieval_gold.jsonl 不存在")
    elif offline:
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
        retrieval = await run_retrieval_eval(
            load_dataset(gold_path), retrieve_fn=_build_store_retrieve_fn(store)
        )
    else:
        from app.deps import vector_client

        await vector_client.start()
        try:
            retrieval = await run_retrieval_eval(
                load_dataset(gold_path),
                retrieve_fn=_build_store_retrieve_fn(vector_client),
            )
        finally:
            await vector_client.close()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "engine": engine or "default",
        "intent": intent,
        "intent_consistency": consistency,
        "guard": guard,
        "tool_selection": tool_selection,
        "hitl_feedback": hitl_feedback,
        "agent_metrics": build_agent_metrics(e2e, tool_selection, hitl_feedback),
        "e2e": e2e,
        "retrieval": retrieval,
        "summary": "",
    }
    report["summary"] = build_summary(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run agent-gateway evaluation harness")
    parser.add_argument(
        "--offline", action="store_true",
        help="零网络零 token：e2e **真跑业务链路**（真 MoAPipeline + 纯正则路由 + mock agent），"
             "但不跑需要模型的 LLM judge（记为 judge未跑，而不是 0 分）",
    )
    parser.add_argument(
        "--engine",
        choices=["fsm", "langgraph"],
        default=None,
        help="override the e2e runner engine (default: whatever ENGINE selects)",
    )
    parser.add_argument("--datasets-dir", type=Path, default=ROOT / "evals" / "datasets")
    parser.add_argument("--report-path", type=Path, default=ROOT / "evals" / "reports" / "latest.json")
    args = parser.parse_args(argv)

    # psycopg 的 async pool 不能用 Windows 默认的 ProactorEventLoop（实测报
    # `Psycopg cannot use the 'ProactorEventLoop' to run in async mode`）→ 向量池
    # 初始化超时 → 检索腿静默退化 → e2e 变成"没有 RAG 上下文的链路"。
    # 只改本 CLI 的循环策略，不动服务进程（服务在 uvicorn / Linux 下另有其循环）。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    report = asyncio.run(run_all(args.offline, args.datasets_dir, engine=args.engine))
    write_report(report, args.report_path)
    print(report["summary"])
    print(f"report written: {args.report_path}")

    failures: list[str] = []
    if report["intent"]["accuracy"] < 0.9:
        failures.append(f"intent accuracy {report['intent']['accuracy']} < 0.9")
    if report["guard"]["deny_recall"] < 0.95:
        failures.append(f"guard deny recall {report['guard']['deny_recall']} < 0.95")
    # 工具选择是离线确定性用例：任何一次不匹配都说明规则/注册表发生了漂移，
    # 是 Agent 级指标里唯一能进 CI 硬门禁的一项。
    if report["tool_selection"]["accuracy"] < 1.0:
        failures.append(
            f"tool selection accuracy {report['tool_selection']['accuracy']} < 1.0 "
            f"(misses: {report['tool_selection']['misses']})"
        )
    # 检索维度必须**真的跑过**，不是"有代码但被 skip"。gold set 与固定语料都随仓库
    # 提交，所以 skip 只有一种解释：有人删了文件或改了名字。这半条门禁不设质量阈值——
    # 23 条 gold set 上一行好坏就是 4.3 个百分点，拿单次实测当阈值只会让 CI 在合法
    # 增删标注时误红。质量下限的加入条件记在 docs/retrieval-evaluation.md。
    retrieval = report.get("retrieval", {})
    if not retrieval.get("total"):
        failures.append(
            "retrieval 维度未运行（gold set 或固定语料缺失）："
            f"{retrieval.get('note', 'unknown')}"
        )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
