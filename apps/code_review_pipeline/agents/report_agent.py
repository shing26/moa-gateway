from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.contract import AgentEnvelope
from app.agents.provider import LLMClient
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm

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

只输出 JSON，不要其他内容。"""


class ReportAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        llm = self._llm or build_code_review_llm()
        system = SYSTEM_PROMPT
        triage = envelope.agent_local_slot.get("triage", {})
        static = envelope.agent_local_slot.get("static_analysis", {})
        semantic = envelope.agent_local_slot.get("semantic_review", {})
        test = envelope.agent_local_slot.get("test_coverage", {})

        user_input = json.dumps({
            "triage": triage,
            "static_analysis": static,
            "semantic_review": semantic,
            "test_coverage": test,
            "pr_title": envelope.agent_local_slot.get("pr_title", ""),
        }, ensure_ascii=False)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        response = await llm.chat(messages)
        return response
