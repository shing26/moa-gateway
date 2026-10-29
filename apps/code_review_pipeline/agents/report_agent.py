from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.contract import AgentEnvelope
from app.agents.provider import LLMClient
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm
from apps.code_review_pipeline.schemas.pipeline import AgentFindingResult, Finding

logger = logging.getLogger("moa.code_review.agents")

SYSTEM_PROMPT = """你是一个代码审查报告生成助手。请综合多个 Agent 的审查结果，生成最终的代码审查报告，输出严格的 JSON：

{
  "findings": [
    {
      "id": "final-001",
      "severity": "critical|high|medium|low|suggestion",
      "category": "security|logic|performance|style|test|architecture",
      "file": "相对路径",
      "line": 行号,
      "title": "问题标题",
      "description": "问题描述",
      "suggestion": "修复建议",
      "confidence": 0.0-1.0,
      "team_specific": true/false,
      "evidence": ["diff:相对路径:行号", "command:pytest 输出摘要"],
      "revision_of": "仅当本 finding 是 Critic revise 后的修订版时，填写原候选 finding_uid",
      "source_agents": ["static_analysis", "semantic_review"]
    }
  ],
  "summary": "总体评估，包含关键指标和主要问题",
  "recommendation": "approve|request_changes|comment",
  "stats": {
    "total_findings": 0,
    "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    "by_category": {"security": 0, "logic": 0, "performance": 0}
  }
}

规则：
1. 合并重复发现（同一文件同一行的问题合并）
2. 按严重程度排序：critical > high > medium > low
3. recommendation 逻辑：
   - critical 或 high 数量 >= 1 -> request_changes
   - medium 数量 >= 3 -> request_changes
   - 否则 -> approve
4. summary 开头用 emoji 表示总体状态：✅/⚠️/❌
5. 如果输入包含 critic_instructions，必须逐条执行并返回完整 findings 列表；
   未要求修改的 finding 原样保留，不得借修订新增未经证据支持的问题。
6. evidence 只能引用 diff、ruff、bandit、rag、command 或 file 中真实存在的结果，
   格式以前缀加冒号开头；没有这类证据时不要发布该 finding。

只输出 JSON，不要其他内容。"""


class ReportAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> AgentFindingResult:
        llm = self._llm or build_code_review_llm()
        system = SYSTEM_PROMPT
        report_inputs = envelope.agent_local_slot.get("report_inputs", {}) or {}
        triage = envelope.agent_local_slot.get(
            "triage", report_inputs.get("triage", {})
        )
        static = envelope.agent_local_slot.get(
            "static_analysis", report_inputs.get("static_analysis", {})
        )
        semantic = envelope.agent_local_slot.get(
            "semantic_review", report_inputs.get("semantic_review", {})
        )
        test = envelope.agent_local_slot.get(
            "test_coverage", report_inputs.get("test_coverage", {})
        )

        user_input = json.dumps(
            {
                "triage": triage,
                "static_analysis": static,
                "semantic_review": semantic,
                "test_coverage": test,
                "pr_title": envelope.agent_local_slot.get("pr_title", ""),
                "candidate_findings": envelope.agent_local_slot.get(
                    "candidate_findings", []
                ),
                "critic_instructions": envelope.agent_local_slot.get(
                    "critic_instructions", []
                ),
            },
            ensure_ascii=False,
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        raw = await llm.chat(messages)
        return _parse_report(raw, trace_id=envelope.trace_id)


def _parse_report(raw: str, *, trace_id: str) -> AgentFindingResult:
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        return AgentFindingResult(
            agent="report",
            trace_id=trace_id,
            findings=(),
            summary="parse report failed",
            recommendation="comment",
            need_human_review=True,
        )

    findings = []
    for item in data.get("findings", []) or []:
        findings.append(
            Finding(
                id=str(item.get("id", "report-finding")),
                severity=str(item.get("severity", "medium")),
                category=str(item.get("category", "general")),
                file=str(item.get("file", "")),
                line=int(item.get("line", 0) or 0),
                title=str(item.get("title", "")),
                description=str(item.get("description", "")),
                suggestion=str(item.get("suggestion", "")),
                confidence=float(item.get("confidence", 0.0) or 0.0),
                team_specific=bool(item.get("team_specific", False)),
                evidence=tuple(item.get("evidence", []) or []),
                source_agents=tuple(item.get("source_agents", []) or []),
                revision_of=(
                    str(item.get("revision_of") or "").strip() or None
                ),
            )
        )
    return AgentFindingResult(
        agent="report",
        trace_id=trace_id,
        findings=tuple(findings),
        summary=str(data.get("summary", "")),
        recommendation=str(data.get("recommendation", "comment")),
        stats=dict(data.get("stats", {}) or {}),
        need_human_review=_infer_hitl(findings),
    )


def _infer_hitl(findings: list[Finding]) -> bool:
    critical_or_high = sum(1 for f in findings if str(f.severity).lower() in ("critical", "high"))
    medium = sum(1 for f in findings if str(f.severity).lower() == "medium")
    return bool(critical_or_high >= 1 or medium >= 3)
