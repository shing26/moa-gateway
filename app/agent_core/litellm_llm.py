from __future__ import annotations

import json
import logging

from app.agent_core.types import DegradeCallback, ReActDecision, TaskResult
from app.agents.provider import LLMClient, LLMConfig
from app.agents.tools import tool_registry

logger = logging.getLogger("moa.agent_core.litellm_llm")


def _notify(on_degrade: DegradeCallback | None, reason: str) -> None:
    """上报一次降级。回调缺省时静默——但调用方**应当**传（ADR-018 决策 7）。"""
    if on_degrade is not None:
        on_degrade(reason)

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
            # 这里返回的是 finish，但它**不是**模型决定收尾——是基础设施崩了。
            # 不带 degraded_reason 的话，`status=ok` 会把"决策失败: ..."当正常回答
            # 交付给用户（ADR-018 决策 7 要修的就是这条路径）。
            return ReActDecision(
                action="finish",
                final_answer=f"决策失败: {exc}",
                degraded_reason=f"decide 调用失败: {exc}",
            )

    async def plan(
        self, *, task: str, on_degrade: DegradeCallback | None = None
    ) -> list[str]:
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
            _notify(on_degrade, "plan 输出不是 JSON 数组")
        except Exception as exc:
            logger.warning("litellm plan failed, fallback to single subtask: %s", exc)
            _notify(on_degrade, f"plan 调用失败: {exc}")
        return [task]

    async def summarize(
        self,
        *,
        task: str,
        plan: list[str],
        results: list[TaskResult],
        on_degrade: DegradeCallback | None = None,
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
            _notify(on_degrade, f"summarize 调用失败: {exc}")
            return "\n".join(lines)

    def _build_decide_prompt(
        self, task: str, subtask: str, observations: list[str]
    ) -> list[dict]:
        # 参数 schema 必须进提示词（ADR-018 决策 4）。此前只给 name + description，
        # 模型看不到参数名与类型，只能在瞎猜——而那正是"参数被拒"的主要来源。
        # 与校验同轮落地，否则只是把"执行崩"换成"被拒但模型仍不知道怎么填"。
        tool_desc = "\n".join(
            "  - {name}: {desc}\n     参数(JSON Schema): {params}".format(
                name=t["function"]["name"],
                desc=t["function"]["description"],
                params=json.dumps(
                    t["function"].get("parameters") or {}, ensure_ascii=False
                ),
            )
            for t in tool_registry.list_schemas()
        )
        parts = [f"当前任务: {task}", f"当前子任务: {subtask}"]
        if observations:
            parts.append("已有观察结果:")
            parts.extend(f"  {o}" for o in observations)
        parts.append(f"\n可用工具:\n{tool_desc}")
        parts.append(
            "\n调用工具时，arguments 只允许使用上面列出的参数名与类型；"
            "未声明的参数会被拒绝。\n"
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
            # 模型没按协议输出 JSON，此前它被当成"收尾答案"直接交付——同样是
            # 把协议失败当正常结果（ADR-018 决策 7）。
            return ReActDecision(
                action="finish",
                final_answer=raw,
                degraded_reason="decide 输出不是合法 JSON",
            )
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