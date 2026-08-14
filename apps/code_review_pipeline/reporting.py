from __future__ import annotations

from typing import Any

REVIEW_SECTIONS = ("triage", "static_analysis", "semantic_review", "test_coverage", "report")


def count_by_severity(result: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attr in REVIEW_SECTIONS:
        section = getattr(result, attr, None)
        if not section:
            continue
        for finding in getattr(section, "findings", ()) or ():
            key = str(getattr(finding, "severity", "unknown")).lower()
            counts[key] = counts.get(key, 0) + 1
    return counts


def format_review_summary(result: Any) -> str:
    counts = count_by_severity(result)
    report = getattr(result, "report", None)
    severity_line = ", ".join(f"{severity}: {count}" for severity, count in sorted(counts.items()))
    lines = [
        f"PR: {result.pr.repo}#{result.pr.pr_number}",
        f"审查结论: {getattr(report, 'recommendation', 'unknown')}",
        f"摘要: {getattr(report, 'summary', '')}",
        f"发现: {sum(counts.values())} 条" + (f"（{severity_line}）" if severity_line else ""),
        f"需要人工复核: {'是' if result.overall_need_human_review else '否'}",
    ]
    return "\n".join(lines)


def build_notification(result: Any, findings_by_severity: dict[str, int]) -> Any:
    from apps.code_review_pipeline.notifications.feishu_notifier import ReviewNotification

    return ReviewNotification(
        trace_id=result.trace_id,
        repo=result.pr.repo,
        pr_number=result.pr.pr_number,
        author=result.pr.author,
        changed_files=len(result.pr.changed_files),
        overall_need_human_review=result.overall_need_human_review,
        findings_by_severity=findings_by_severity,
        report=getattr(result, "report", None),
    )
