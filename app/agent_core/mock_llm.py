from __future__ import annotations

import re

from app.agent_core.types import ReActDecision, TaskResult

# 规则：(正则, 工具名, 参数名 或 "")
# 匹配到就调用对应工具；参数名非空时把匹配组 2 作为参数值。
_TOOL_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"(搜索|查一下|查询|检索|找找|知识库)[:：]?\s*(.+)$", re.IGNORECASE),
        "knowledge_search",
        "query",
    ),
    (
        re.compile(r"(计算|算一下|求值|帮我算)[:：]?\s*(.+)$", re.IGNORECASE),
        "calculator",
        "expression",
    ),
    (
        re.compile(r"(时间|几点|日期|现在|星期|今天)", re.IGNORECASE),
        "current_time",
        "",
    ),
    (
        re.compile(r"(记(?:录|一下|下|住)|备忘|添加笔记)[:：]?\s*(.+)$", re.IGNORECASE),
        "add_note",
        "note",
    ),
    (
        re.compile(r"(查笔记|看笔记|我的笔记|查看笔记|笔记列表|读取笔记)", re.IGNORECASE),
        "get_notes",
        "",
    ),
    (
        re.compile(r"(列出?文档|文档列表|知识库文档|所有文档)", re.IGNORECASE),
        "list_documents",
        "",
    ),
]

_SPLIT_SEPARATORS = [
    re.compile(r"(?:并且|然后|接着|再|；|;|\n)"),
    re.compile(r"(?:[。.！!？?])"),
]


class MockTaskLLM:
    """离线确定性 Mock LLM：基于规则做规划/决策/汇总，不依赖任何外部 LLM 或网络。

    这是"可离线跑的 Agent 骨架"的决策层——ReAct 循环、工具执行、
    多轮观察都是真实实现，只有"下一个动作选什么"由规则模拟。
    """

    def __init__(self, tool_rules: list | None = None) -> None:
        self._rules = tool_rules if tool_rules is not None else _TOOL_RULES

    async def decide(
        self, *, task: str, subtask: str, observations: list[str]
    ) -> ReActDecision:
        # 已有工具执行结果 → 汇总回答（观察驱动结束）
        if observations:
            return ReActDecision(
                action="finish",
                final_answer=self._summarize_observations(subtask, observations),
                note="基于工具结果汇总回答",
            )
        # 首次决策 → 匹配工具规则
        body = subtask or task
        for pattern, tool_name, arg_key in self._rules:
            m = pattern.search(body)
            if m:
                if arg_key:
                    raw = m.group(2) if m.lastindex and m.lastindex >= 2 else body
                    args = {arg_key: raw.strip()}
                else:
                    args = {}
                return ReActDecision(
                    action="call_tool",
                    tool_name=tool_name,
                    arguments=args,
                    note=f"调用 {tool_name} 获取信息",
                )
        return ReActDecision(
            action="finish",
            final_answer=self._generic_reply(subtask or task),
            note="无法匹配工具，直接回复",
        )

    async def plan(self, *, task: str) -> list[str]:
        """将任务拆解为有序子任务列表（按连接词/标点切分）。"""
        parts = [task]
        for sep in _SPLIT_SEPARATORS:
            if len(parts) <= 1:
                parts = [p for p in sep.split(task) if p and p.strip()]
        cleaned = [p.strip().lstrip("。.!！?？;；，,和并且然后接着再").strip() for p in parts if p and p.strip()]
        return cleaned or [task]

    async def summarize(
        self, *, task: str, plan: list[str], results: list[TaskResult]
    ) -> str:
        lines = [f"## 任务完成报告\n\n**任务**: {task}"]
        if plan:
            lines.append(f"\n**执行计划**: {len(plan)} 个步骤\n")
        for i, (sub, res) in enumerate(zip(plan, results)):
            lines.append(f"### 步骤 {i + 1}: {sub}")
            lines.append(f"**工具调用**: {res.tool_calls} 次")
            lines.append(res.answer)
        return "\n\n".join(lines)

    def _summarize_observations(self, subtask: str, observations: list[str]) -> str:
        lines = [f"关于「{subtask}」的结果："]
        lines.extend(observations)
        return "\n\n".join(lines)

    @staticmethod
    def _generic_reply(subtask: str) -> str:
        return (
            f"已收到你的请求「{subtask}」。你可以让我："
            "搜索知识库、计算表达式、查时间、记笔记/查笔记、列出文档。"
        )