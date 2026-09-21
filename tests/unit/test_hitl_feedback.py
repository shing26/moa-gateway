"""HITL 决策回流采集器：trace 配对语义、合成流量排除、指标口径。"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.collect_hitl_feedback import build_feedback, write_outputs


def _entry(
    trace: str,
    action: str,
    *,
    session: str = "oc_real",
    ts: str = "2026-09-21T10:00:00+00:00",
    input_preview: str = "",
    output_preview: str = "",
    policy_hits: list | None = None,
) -> dict:
    return {
        "trace_id": trace,
        "session_id": session,
        "guard_action": action,
        "timestamp": ts,
        "agent_name": "general",
        "intent": "assistant",
        "input_preview": input_preview,
        "output_preview": output_preview,
        "policy_hits": policy_hits or [],
    }


def test_matched_pair_becomes_a_case_with_previews_and_latency():
    entries = [
        _entry(
            "t1", "review", ts="2026-09-21T10:00:00+00:00",
            input_preview="报价 1999 元/月", output_preview="您好，我们提供本方案报价为1999元/月",
            policy_hits=["policy.compliance.no_price_commitment"] * 2,  # 守卫按命中次数返回
        ),
        _entry("t1", "hitl_approve", ts="2026-09-21T10:00:02+00:00"),
    ]
    result = build_feedback(entries)
    assert len(result["cases"]) == 1
    case = result["cases"][0]
    assert case["decision"] == "approve"
    assert case["input_preview"] == "报价 1999 元/月"
    assert case["policy_hits"] == ["policy.compliance.no_price_commitment"]  # 去重
    assert case["decision_latency_ms"] == 2000.0
    assert result["meta"]["approve_rate"] == 1.0


def test_decision_without_trigger_is_skipped_and_counted():
    # ②a 之前的历史决策：审计 trace 各不相同，无法配对 → 不产出用例但如实计数
    entries = [_entry("t2", "hitl_approve")]
    result = build_feedback(entries)
    assert result["cases"] == []
    assert result["meta"]["unmatched_decisions"] == 1


def test_synthetic_sessions_excluded_from_metrics_but_still_paired():
    entries = [
        _entry("p1", "review", session="probe-seed-a"),
        _entry("p1", "hitl_reject", session="probe-seed-a"),
        _entry("r1", "review", session="oc_real"),
        _entry("r1", "hitl_approve", session="oc_real"),
    ]
    result = build_feedback(entries, excluded_session_prefixes=("probe",))
    decisions = {c["session_id"] for c in result["cases"]}
    assert decisions == {"oc_real"}, "合成流量不应进入数据集"
    assert result["meta"]["excluded_synthetic_traces"] == 1
    assert result["meta"]["requests"] == 1, "请求分母按去重 trace 计且排除合成流量"


def test_intervention_rate_uses_distinct_traces_as_denominator():
    entries = [
        # 一次普通请求写了 3 条审计（同 trace）
        _entry("q1", "allow"),
        _entry("q1", "allow", ts="2026-09-21T10:00:01+00:00"),
        _entry("q1", "allow", ts="2026-09-21T10:00:02+00:00"),
        # 一次被拦截并被人放行
        _entry("q2", "review"),
        _entry("q2", "hitl_approve", ts="2026-09-21T10:00:05+00:00"),
    ]
    result = build_feedback(entries)
    meta = result["meta"]
    assert meta["requests"] == 2  # 不是 5
    assert meta["human_intervention_rate"] == 0.5
    assert meta["guard_interceptions"] == 1
    assert meta["undecided_interceptions"] == 0


def test_undecided_interception_is_reported():
    result = build_feedback([_entry("t3", "review")])
    assert result["meta"]["undecided_interceptions"] == 1
    assert result["meta"]["approve_rate"] == 0.0


def test_write_outputs_dedupes_by_case_id(tmp_path: Path):
    result = build_feedback([
        _entry("t4", "review"),
        _entry("t4", "hitl_approve", ts="2026-09-21T10:00:01+00:00"),
    ])
    added, total = write_outputs(result, tmp_path)
    assert (added, total) == (1, 1)
    # 再写一次：按 id 去重，不重复追加
    added2, total2 = write_outputs(result, tmp_path)
    assert (added2, total2) == (0, 1)
    rows = [json.loads(line) for line in (tmp_path / "hitl_feedback.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1
    meta = json.loads((tmp_path / "hitl_feedback.meta.json").read_text(encoding="utf-8"))
    assert meta["matched_decisions"] == 1
