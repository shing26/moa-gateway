from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.guard.guard_service import guard_service
from app.guard.redteam import run
from scripts.redteam.generate_cases import CASES_PATH, CATEGORY_LABELS, generate

REPORT_PATH = Path(__file__).resolve().parent / "report.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def evaluator(text: str) -> tuple[str, tuple[str, ...]]:
    verdict, policy_ids = guard_service.evaluate_output(text, intent="assistant")
    return verdict.action, policy_ids


def load_cases() -> list[dict]:
    if not CASES_PATH.exists():
        generate()
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return payload["cases"]


def format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def summarize(report) -> dict:
    return {
        "total": report.total,
        "blocked": report.true_positive,
        "true_positive": report.true_positive,
        "false_negative": report.false_negative,
        "false_positive": report.false_positive,
        "true_negative": report.true_negative,
        "recall": report.recall,
        "precision": report.precision,
        "false_positive_rate": report.false_positive_rate,
    }


def write_report(report, summary: dict) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "by_category": {cat: vars(stats) for cat, stats in report.by_category.items()},
        "category_labels": CATEGORY_LABELS,
        "details": [vars(d) for d in report.details],
    }
    REPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_summary(report) -> None:
    summary = summarize(report)
    print("===== 红队测试报告 =====")
    print(f"总用例数: {summary['total']}")
    print(f"期望拦截: {summary['true_positive'] + summary['false_negative']}")
    print(f"实际拦截(TP): {summary['true_positive']}")
    print(f"漏网(FN): {summary['false_negative']}")
    print(f"误拦截(FP): {summary['false_positive']}")
    print(f"正常放行(TN): {summary['true_negative']}")
    print(f"召回率: {format_pct(summary['recall'])}")
    print(f"精确率: {format_pct(summary['precision'])}")
    print(f"误拦截率: {format_pct(summary['false_positive_rate'])}")
    print("--- 分类明细 ---")
    for cat, stats in report.by_category.items():
        label = CATEGORY_LABELS.get(cat, cat)
        print(
            f"{cat}({label})  total={stats.total}  TP={stats.true_positive}  "
            f"FN={stats.false_negative}  FP={stats.false_positive}  TN={stats.true_negative}  "
            f"召回={format_pct(stats.recall)}  精确={format_pct(stats.precision)}  "
            f"误拦={format_pct(stats.false_positive_rate)}"
        )
    print(f"报告已写入: {REPORT_PATH}")


def main() -> None:
    cases = load_cases()
    report = run(cases, evaluator)
    summary = summarize(report)
    write_report(report, summary)
    print_summary(report)


if __name__ == "__main__":
    main()
