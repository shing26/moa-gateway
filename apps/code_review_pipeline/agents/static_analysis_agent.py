from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from app.agents.contract import AgentEnvelope
from app.agents.provider import LLMClient
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm
from apps.code_review_pipeline.schemas.pipeline import Finding
from apps.code_review_pipeline.agents.static_tool import run_ruff_on_files, RuffExecutionError

logger = logging.getLogger("moa.code_review.agents")

SYSTEM_PROMPT = """\
你是一位资深代码审查专家。你会收到一批静态扫描工具（Ruff）产出的告警列表，以及对应的代码 Diff 片段。

你的职责不是简单复述这些告警，而是做“研判 + 提纯”：
1. 区分“真正影响 PR 质量的致命问题”和“无伤大雅的风格噪音”。
2. 对真正重要的问题重新评估严重性，并给出具体的重构建议。
3. 忽略与本 PR 变更无关的告警，或明显是误报的告警。
4. 必须只引用 Diff 中明确出现的行号，禁止推测或引用未修改的代码行。

输出严格的 JSON，格式如下：
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
  "summary": "总体评估，1-2 句话",
  "recommendation": "给开发者的优先处理建议"
}

只输出 JSON，不要其他内容。"""


def _extract_tool_findings(envelope: AgentEnvelope) -> list[dict[str, Any]]:
    """Extract ruff findings from agent_local_slot if already populated."""
    slot = envelope.agent_local_slot or {}
    raw = slot.get("ruff_findings")
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _build_diff_context(envelope: AgentEnvelope) -> str:
    """Build a compact diff context for LLM judgment."""
    diff = (envelope.agent_local_slot or {}).get("diff", "")
    if not diff:
        return ""
    # Truncate extremely large diffs to protect context window.
    return diff[:12000]


def _build_tool_context(findings: Sequence[dict[str, Any]]) -> str:
    """Build a compact tool findings context for LLM judgment."""
    if not findings:
        return ""
    lines = [f"Ruff produced {len(findings)} finding(s):"]
    for idx, item in enumerate(findings, start=1):
        code = item.get("code", "")
        message = item.get("message", "")
        filename = item.get("filename", "")
        location = item.get("location") or {}
        row = location.get("row", 0)
        column = location.get("column", 0)
        fix = item.get("fix") or {}
        fix_message = fix.get("message") if isinstance(fix, dict) else ""
        lines.append(
            f"{idx}. [{code}] {filename}:{row}:{column} - {message}"
            + (f" | fix: {fix_message}" if fix_message else "")
        )
    return "\n".join(lines)


class StaticAnalysisAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        llm = self._llm or build_code_review_llm()
        slot = envelope.agent_local_slot or {}

        # Step 1: collect changed Python files from PR context if available.
        changed_files: list[dict[str, str]] = slot.get("changed_python_files") or []
        # Fallback: if the pipeline only stored changed_files metadata without content,
        # we cannot run ruff here; return a structured no-op result.
        if not changed_files:
            return json.dumps(
                {
                    "findings": [],
                    "summary": "没有可扫描的变更 Python 文件，静态分析已跳过。",
                    "recommendation": "",
                },
                ensure_ascii=False,
            )

        # Step 2: run ruff on the changed files only.
        try:
            ruff_findings = run_ruff_on_files(changed_files)
        except RuffExecutionError as exc:
            logger.error("ruff execution failed: %s", exc)
            return json.dumps(
                {
                    "findings": [],
                    "summary": f"静态扫描执行失败：{exc}",
                    "recommendation": "请检查环境是否安装了 ruff，或稍后重试。",
                },
                ensure_ascii=False,
            )

        tool_context = _build_tool_context(ruff_findings)
        diff_context = _build_diff_context(envelope)

        user_input_parts = []
        if tool_context:
            user_input_parts.append(tool_context)
        if diff_context:
            user_input_parts.append("以下是本次 PR 的 Diff 片段：\n" + diff_context)
        user_input = "\n\n".join(user_input_parts) if user_input_parts else "未找到静态扫描结果或 Diff。"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_input},
        ]

        # Step 3: let the LLM filter and refine the tool findings.
        response = await llm.chat(messages)
        return response
