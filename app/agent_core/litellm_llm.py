from __future__ import annotations

import json
import logging

from app.agent_core.types import ReActDecision, TaskResult
from app.agents.provider import LLMClient, LLMConfig
from app.agents.tools import tool_registry

logger = logging.getLogger("moa.agent_core.litellm_llm")

_SYSTEM_PROMPT = (
    "你是一个自主任务 Agent。你会收到一个任务，需要：\n"
    "1. 规划：把任务拆成有序子任务；\n"
    "2. 对每个子任务执行 ReAct 循环：决策（调用工具或结束），"
    "工具结果会回传给你，根据观察继续或收尾；\n"
    "3. 汇总所有子任务结果，输出简洁完整的中文汇报。\n\n"
    "每次决策必须以 JSON 输出：\n"
    '{"action": "call_tool", "tool_name": "...", "arguments": {...}} 调用工具\n'
    '{"action": "finish", "final_answer": "..."} 结束当前子任务'
)


class LiteLLMTaskLLM:
    """接真实 LLM 的任务决策层（可选；未配置凭据时抛错由调用方回退 Mock）。"""

    def __init__(self, client: LLMClient | None = None) -> None:
        if client is None:
            config = LLMConfig.from_env("LLM")
            if not config.api_key:
                raise RuntimeError("LLM credentials not configured for LiteLLM task LLM")
            client = LLMClient(config)
        self._client = client

    async def decide(
        self, *, task: str, subtask: str, observations: list[str]
    ) -> ReActDecision:
        prompt = self._build_decide_prompt(task, subtask, observations)
        try:
            raw = await self._client.chat(prompt)
            return self._parse_decision(raw)
        except Exception as exc:
            logger.warning("litellm decide failed: %s", exc)
            return ReActDecision(
                action="finish",
                final_answer=f"决策失败: {exc}",
            )

    async def plan(self, *, task: str) -> list[str]:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"请将以下任务拆解为有序的子任务列表，用 JSON 数组返回：\n\n{task}"
                ),
            },
        ]
        try:
            raw = await self._client.chat(messages)
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception as exc:
            logger.warning("litellm plan failed, fallback to single subtask: %s", exc)
        return [task]

    async def summarize(
        self, *, task: str, plan: list[str], results: list[TaskResult]
    ) -> str:
        lines = [f"任务: {task}"]
        for sub, res in zip(plan, results):
            lines.append(f"步骤「{sub}」结果:\n{res.answer[:500]}")
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "请汇总以下任务执行结果，生成简洁完整的中文汇报：\n\n"
                    + "\n".join(lines)
                ),
            },
        ]
        try:
            return await self._client.chat(messages)
        except Exception as exc:
            logger.warning("litellm summarize failed: %s", exc)
            return "\n".join(lines)

    def _build_decide_prompt(
        self, task: str, subtask: str, observations: list[str]
    ) -> list[dict]:
        tool_desc = "\n".join(
            f"  - {t['function']['name']}: {t['function']['description']}"
            for t in tool_registry.list_schemas()
        )
        parts = [f"当前任务: {task}", f"当前子任务: {subtask}"]
        if observations:
            parts.append("已有观察结果:")
            parts.extend(f"  {o}" for o in observations)
        parts.append(f"\n可用工具:\n{tool_desc}")
        parts.append(
            '\n请输出 JSON 决策，例如 {"action": "call_tool", '
            '"tool_name": "current_time", "arguments": {}}'
        )
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ]

    def _parse_decision(self, raw: str) -> ReActDecision:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if "```" in cleaned:
                cleaned = cleaned.split("```")[0]
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return ReActDecision(action="finish", final_answer=raw)
        action = data.get("action", "finish")
        if action == "call_tool":
            return ReActDecision(
                action="call_tool",
                tool_name=data.get("tool_name", ""),
                arguments=data.get("arguments") or {},
                note=data.get("note", f"调用 {data.get('tool_name', '?')}"),
            )
        return ReActDecision(
            action="finish",
            final_answer=data.get("final_answer", raw),
        )