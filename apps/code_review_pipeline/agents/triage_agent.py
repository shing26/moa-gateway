from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.contract import AgentEnvelope
from app.agents.provider import LLMClient
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm

logger = logging.getLogger("moa.code_review.agents")

SYSTEM_PROMPT = """你是一个代码审查分类助手。请分析这个 Pull Request，输出严格的 JSON：

{
  "pr_type": "feature|bugfix|refactor|chore|docs|test",
  "priority": "P0|P1|P2|P3",
  "review_depth": "skip|standard|deep",
  "reason": "分类理由，1-2 句话"
}

规则：
- feature: 新功能开发
- bugfix: 修复缺陷
- refactor: 代码重构，无业务逻辑变化
- chore: 构建/依赖/配置变更
- docs: 仅文档变更
- test: 仅测试代码

- P0: 核心路径/安全相关/支付等关键模块
- P1: 重要功能，影响用户体验
- P2: 一般功能
- P3: 低优先级优化

- skip: 文档/格式/注释等无需深入审查
- standard: 常规审查
- deep: 核心模块/安全敏感/架构变更

只输出 JSON，不要其他内容。"""


class TriageAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        llm = self._llm or build_code_review_llm()
        system = SYSTEM_PROMPT
        user_input = envelope.user_raw_input or json.dumps({
            "title": envelope.agent_local_slot.get("pr_title", ""),
            "changed_files": envelope.agent_local_slot.get("changed_files", []),
        }, ensure_ascii=False)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        response = await llm.chat(messages)
        return response
