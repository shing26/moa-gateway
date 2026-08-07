from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.contract import AgentEnvelope
from app.agents.provider import LLMClient
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm

logger = logging.getLogger("moa.code_review.agents")

SYSTEM_PROMPT = """你是一个静态代码分析助手。请分析以下代码 diff，找出潜在问题，输出严格的 JSON：

{
  "findings": [
    {
      "id": "static-001",
      "severity": "critical|high|medium|low",
      "category": "security|performance|style|bug|maintainability",
      "file": "相对路径",
      "line": 行号,
      "title": "问题标题",
      "description": "问题描述",
      "suggestion": "修复建议",
      "confidence": 0.0-1.0
    }
  ],
  "summary": "总体评估，1-2 句话"
}

重点检查：
1. 硬编码密钥/密码/Token
2. SQL 注入、XSS、路径遍历等安全风险
3. 空指针/未处理异常
4. 资源泄漏（未关闭文件/连接）
5. 明显的性能问题（循环内数据库查询等）
6. 代码重复/复杂度超标

注意：你只能引用 diff 中明确出现的行号，禁止推测或引用未修改的代码行。
只输出 JSON，不要其他内容。"""


class StaticAnalysisAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        llm = self._llm or build_code_review_llm()
        system = SYSTEM_PROMPT
        user_input = envelope.user_raw_input or json.dumps({
            "diff": envelope.agent_local_slot.get("diff", ""),
            "changed_files": envelope.agent_local_slot.get("changed_files", []),
        }, ensure_ascii=False)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        response = await llm.chat(messages)
        return response
