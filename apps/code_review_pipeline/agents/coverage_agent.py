"""代码审查流水线里的“测试覆盖率”分析 agent。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.contract import AgentEnvelope
from app.agents.provider import LLMClient
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm

logger = logging.getLogger("moa.code_review.agents")

SYSTEM_PROMPT = """你是一个测试覆盖率分析助手。请分析以下代码 diff，输出严格的 JSON：

{
  "findings": [
    {
      "id": "test-001",
      "severity": "medium|low",
      "category": "test_coverage|test_quality|missing_scenario",
      "file": "相对路径",
      "line": 行号,
      "title": "问题标题",
      "description": "问题描述",
      "suggestion": "测试建议",
      "confidence": 0.0-1.0
    }
  ],
  "summary": "测试评估，1-2 句话",
  "recommendation": "adequate|needs_improvement|insufficient"
}

检查重点：
1. 新增代码是否包含对应测试
2. 边界条件是否被覆盖
3. 异常处理是否有测试
4. 现有测试是否需要更新
5. 建议的测试场景

注意：你只能引用 diff 中明确出现的行号，禁止推测或引用未修改的代码行。
只输出 JSON，不要其他内容。"""


class TestCoverageAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        llm = self._llm or build_code_review_llm()
        system = SYSTEM_PROMPT
        user_input = envelope.user_raw_input or json.dumps({
            "diff": envelope.agent_local_slot.get("diff", ""),
            "changed_files": envelope.agent_local_slot.get("changed_files", []),
            "existing_tests": envelope.agent_local_slot.get("existing_tests", []),
        }, ensure_ascii=False)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        response = await llm.chat(messages)
        return response
