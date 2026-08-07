from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.contract import AgentEnvelope
from app.agents.provider import LLMClient
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm

logger = logging.getLogger("moa.code_review.agents")

SYSTEM_PROMPT = """你是一个资深架构师，负责代码语义审查。请分析以下代码 diff，输出严格的 JSON：

{
  "findings": [
    {
      "id": "semantic-001",
      "severity": "critical|high|medium|low",
      "category": "logic|architecture|security|performance|api_design",
      "file": "相对路径",
      "line": 行号,
      "title": "问题标题",
      "description": "问题描述",
      "suggestion": "修复建议",
      "confidence": 0.0-1.0,
      "team_specific": true/false,
      "evidence": ["rag:pattern:001", "rag:pr:456"]
    }
  ],
  "summary": "总体评估，1-2 句话",
  "recommendation": "approve|request_changes|comment"
}

审查维度：
1. 业务逻辑正确性
2. 边界条件处理
3. 并发安全
4. 接口设计合理性
5. 与系统其他部分的兼容性
6. 团队历史规范和架构决策（如提供）

注意：你只能引用 diff 中明确出现的行号，禁止推测或引用未修改的代码行。
只输出 JSON，不要其他内容。"""


class SemanticReviewAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        llm = self._llm or build_code_review_llm()
        system = SYSTEM_PROMPT
        rag_context = envelope.agent_local_slot.get("rag_context", {})
        user_input = envelope.user_raw_input or json.dumps({
            "diff": envelope.agent_local_slot.get("diff", ""),
            "pr_title": envelope.agent_local_slot.get("pr_title", ""),
            "team_patterns": rag_context.get("patterns", []),
            "historical_prs": rag_context.get("historical_prs", []),
        }, ensure_ascii=False)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        response = await llm.chat(messages)
        return response
