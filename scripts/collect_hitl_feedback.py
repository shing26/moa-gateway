"""HITL 决策回流：把真人审批结果采集为可评测的数据集。

审计条目现在共享请求 trace（``app/middleware/request_logger.py`` 的
``bind_trace``），因此"哪次拦截 → 人做了什么决定"可以按 trace 对齐：

- **触发条目**：``guard_action == "review"``（策略命中、挂起待人工审批）
- **决策条目**：``guard_action in {"hitl_approve", "hitl_reject"}``（真人在卡片上点的）

产出（默认写入 evals/datasets/）：

- ``hitl_feedback.jsonl``：每条一个"已人工裁决"的用例，按 trace 去重，
  含输入/输出摘要、命中策略、决策与决策耗时——这是把 HITL 从"刹车"
  变成"标注管道"的原料。
- ``hitl_feedback.meta.json``：聚合指标。``human_intervention_rate`` =
  需人工决策的请求数 / 审计窗口内的请求总数（真实业务指标）；
  ``approve_rate`` = 放行 / 已裁决。

用法::

    uv run python scripts/collect_hitl_feedback.py
    uv run python scripts/collect_hitl_feedback.py --logs-dir logs --datasets-dir evals/datasets
"""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime, timezone
from typing import Any

TRIGGER_ACTIONS = {"review"}
DECISION_ACTIONS = {"hitl_approve", "hitl_reject"}
# 合成流量（评测/探针/测试夹具）会把真实指标稀释掉，默认排除
DEFAULT_EXCLUDED_SESSION_PREFIXES = ("probe", "eval", "test", "dash-test")

# 已裁决用例写入时间窗口的 trace 前缀


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_audit_entries(logs_dir: pathlib.Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(logs_dir.glob("audit-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                entries.append(data)
    return entries


def _is_synthetic(session_id: str, prefixes: tuple[str, ...]) -> bool:
    sid = (session_id or "").lower()
    return any(sid.startswith(p.lower()) for p in prefixes)


def _field(entry: dict[str, Any], key: str) -> Any:
    """审计 JSONL 的字段是扁平的（WAL 白名单），旧记录可能缺 key；兼容嵌套 extra。"""
    extra = entry.get("extra")
    if isinstance(extra, dict) and extra.get(key) not in (None, "", [], ()):
        return extra[key]
    return entry.get(key, "")


def _policy_hits(entry: dict[str, Any]) -> list[str]:
    """命中策略清单（去重保序）。

    守卫按"命中的正则次数"返回 policy_ids，同一策略可能重复出现
    （如价格策略命中两条正则）；数据集里按策略去重更有意义。
    """
    hits = _field(entry, "policy_hits")
    if isinstance(hits, (list, tuple)) and hits:
        seen: list[str] = []
        for hit in hits:
            text = str(hit)
            if text and text not in seen:
                seen.append(text)
        return seen
    reason = str(_field(entry, "guard_reason") or "")
    return [reason] if reason else []


def build_feedback(
    entries: list[dict[str, Any]],
    *,
    excluded_session_prefixes: tuple[str, ...] = DEFAULT_EXCLUDED_SESSION_PREFIXES,
) -> dict[str, Any]:
    """按 trace 关联拦截与决策，返回 {cases, meta, undecided}。

    只有同时找到"触发条目（review）"与"决策条目（hitl_approve/reject）"的
    trace 才产出用例——否则用例没有输入/输出可评，只会污染数据集。
    请求分母按 **去重后的 trace 数** 计（一次请求可能写多条审计）。
    """
    triggers: dict[str, dict[str, Any]] = {}
    decisions: dict[str, dict[str, Any]] = {}
    traces: set[str] = set()
    synthetic_traces: set[str] = set()
    for entry in entries:
        trace = str(entry.get("trace_id", ""))
        if trace:
            traces.add(trace)
        if _is_synthetic(str(entry.get("session_id", "")), excluded_session_prefixes):
            if trace:
                synthetic_traces.add(trace)
            continue
        action = str(entry.get("guard_action", ""))
        if not trace:
            continue
        if action in TRIGGER_ACTIONS:
            # 同一 trace 可能有多条（重试），保留最早一条作为触发点
            triggers.setdefault(trace, entry)
        elif action in DECISION_ACTIONS:
            decisions[trace] = entry

    cases: list[dict[str, Any]] = []
    unmatched_decisions = 0
    for trace, decision in decisions.items():
        trigger = triggers.get(trace)
        if trigger is None:
            # ②a（审计 trace 贯通）之前的历史决策无法关联到触发请求，跳过
            unmatched_decisions += 1
            continue
        decided_at = _parse_ts(decision.get("timestamp"))
        triggered_at = _parse_ts(trigger.get("timestamp"))
        latency_ms = (
            round((decided_at - triggered_at).total_seconds() * 1000, 1)
            if decided_at and triggered_at
            else None
        )
        cases.append({
            "id": f"hitl-{trace}",
            "trace_id": trace,
            "session_id": decision.get("session_id", ""),
            "decision": "approve" if decision.get("guard_action") == "hitl_approve" else "reject",
            "decided_at": decision.get("timestamp", ""),
            "decision_latency_ms": latency_ms,
            "input_preview": str(_field(trigger, "input_preview") or ""),
            "output_preview": str(_field(trigger, "output_preview") or ""),
            "policy_hits": _policy_hits(trigger),
            "agent_name": decision.get("agent_name", ""),
            "intent": decision.get("intent", ""),
        })

    approved = sum(1 for c in cases if c["decision"] == "approve")
    decided = len(cases)
    undecided = sorted(set(triggers) - set(decisions))
    real_requests = len(traces - synthetic_traces)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "audit_entries": len(entries),
        "requests": real_requests,
        "excluded_synthetic_traces": len(synthetic_traces),
        "guard_interceptions": len(triggers),
        "matched_decisions": decided,
        "unmatched_decisions": unmatched_decisions,
        "undecided_interceptions": len(undecided),
        "approve_count": approved,
        "reject_count": decided - approved,
        "approve_rate": round(approved / decided, 4) if decided else 0.0,
        "human_intervention_rate": round(decided / real_requests, 4) if real_requests else 0.0,
        "latencies_ms": [c["decision_latency_ms"] for c in cases if c["decision_latency_ms"]],
    }
    return {"cases": cases, "meta": meta, "undecided": undecided}


def write_outputs(result: dict[str, Any], datasets_dir: pathlib.Path) -> tuple[int, int]:
    """追加写入数据集（按 id 去重），并覆盖 meta。返回 (新增, 总数)。"""
    datasets_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = datasets_dir / "hitl_feedback.jsonl"
    existing_ids: set[str] = set()
    existing_rows: list[str] = []
    if dataset_path.exists():
        for line in dataset_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            existing_rows.append(line)
            try:
                existing_ids.add(str(json.loads(line).get("id", "")))
            except json.JSONDecodeError:
                continue
    added = 0
    for case in result["cases"]:
        if case["id"] in existing_ids:
            continue
        existing_rows.append(json.dumps(case, ensure_ascii=False))
        existing_ids.add(case["id"])
        added += 1
    dataset_path.write_text("\n".join(existing_rows) + "\n", encoding="utf-8")
    (datasets_dir / "hitl_feedback.meta.json").write_text(
        json.dumps(result["meta"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return added, len(existing_ids)


def main(argv: list[str] | None = None) -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Collect human HITL decisions into an eval dataset")
    parser.add_argument("--logs-dir", type=pathlib.Path, default=root / "logs")
    parser.add_argument("--datasets-dir", type=pathlib.Path, default=root / "evals" / "datasets")
    parser.add_argument(
        "--exclude-session-prefixes",
        default=",".join(DEFAULT_EXCLUDED_SESSION_PREFIXES),
        help="逗号分隔的会话前缀，命中视为合成流量（评测/探针）不计入指标；传空串则全部计入",
    )
    parser.add_argument("--note", default="", help="写入 meta 的备注（如本次采集的来源说明）")
    args = parser.parse_args(argv)

    prefixes = tuple(
        p.strip() for p in str(args.exclude_session_prefixes).split(",") if p.strip()
    )
    entries = load_audit_entries(args.logs_dir)
    result = build_feedback(entries, excluded_session_prefixes=prefixes)
    if args.note:
        result["meta"]["note"] = args.note
    added, total = write_outputs(result, args.datasets_dir)
    meta = result["meta"]
    print(
        f"audit entries={meta['audit_entries']} requests={meta['requests']} "
        f"interceptions={meta['guard_interceptions']} matched={meta['matched_decisions']} "
        f"unmatched={meta['unmatched_decisions']} (pre-trace-binding decisions)"
    )
    print(
        f"approve_rate={meta['approve_rate']} human_intervention_rate={meta['human_intervention_rate']} "
        f"undecided={meta['undecided_interceptions']} excluded_synthetic={meta['excluded_synthetic_traces']}"
    )
    print(f"dataset: +{added} new cases ({total} total) -> {args.datasets_dir / 'hitl_feedback.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
