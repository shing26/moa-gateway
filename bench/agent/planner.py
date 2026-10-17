"""L2 的任务分解与 handoff（§5 四档表）：planner 给 worker 的**自带上下文的子任务**。

这一档要单独跑通的只有一课：**上下文怎么从规划者传到执行者。**
§5.1 已经把专家接到工具循环上，所以 L2 不再发明执行体；先把"分解 → 交接"这条
缝跑通，再谈并发（L3）。

为什么子任务要带 ``context``
---------------------------
§5 的判据是"规划出的子任务带够了上下文、执行者不必回头问"。所以子任务不是一个
光秃秃的动词短语（"改 auth.py"），而是 planner 认为足够开工的一组事实：失败长什么
样、根因在哪、该动哪个文件哪一行。worker 拿到的**唯一**输入就是这条指令加它的
context，外加前面子任务的产出——它不会自己去读 planner 的脑子。

``depends_on`` 为什么保留但只在 L2 里记账
----------------------------------------
自建 FSM（``app/orchestration/collaboration.py``）用 ``depends_on`` 表达 Send
扇出后的依赖。L2 只有一个 worker、串行执行，所有前序产出本来就都在手里，依赖信息
暂时没有可执行的含义。字段留着是因为它属于 handoff 的词汇（L3 定向重跑要用），
但这里的 L2 不假装靠它做调度——"串行即满足"就直说。

解析失败要**报错**，不能自己编一份计划
------------------------------------
planner 的输出是 LLM 的自由文本。读不出结构化计划时，编一份"看起来像"的计划会让
整档跑在一个没人写过的计划上——那比直接失败更坏。所以 ``parse_plan`` 读不出来就抛
``PlanParseError``，由 L2 如实记成 ``error``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from bench.agent.critic import APPROVE, REVISE
from bench.agent.parsing import nonnegative_indices


@dataclass(frozen=True)
class Subtask:
    """一个交给 worker 的子任务。``context`` 是 planner 附上的开工信息。"""

    index: int
    instruction: str
    context: str
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class Plan:
    """一次分解的全部子任务，按下标有序。"""

    subtasks: tuple[Subtask, ...]


class PlanParseError(ValueError):
    """planner 的输出里读不出结构化计划。"""


def parse_plan(text: str, *, max_subtasks: int = 4) -> Plan:
    """把 planner 的终局解析成 ``Plan``。读不出来抛 ``PlanParseError``。"""
    raw = (text or "").strip()
    if not raw:
        raise PlanParseError("planner 没有给出任何输出")

    items = _json_items(raw)
    if not items:
        raise PlanParseError(f"planner 的输出里读不出子任务: {raw[:200]!r}")

    subtasks: list[Subtask] = []
    upper = max_subtasks if max_subtasks > 0 else len(items)
    for item in items[:upper]:
        subtasks.append(_to_subtask(item, index=len(subtasks)))
    if not subtasks:
        raise PlanParseError("计划里没有一个可执行的子任务")
    return Plan(subtasks=tuple(subtasks))


def _json_items(text: str) -> list[object]:
    blob = _extract_json(text)
    if blob is None:
        return []
    try:
        payload = json.loads(blob)
    except (TypeError, ValueError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("subtasks") or payload.get("plan") or []
    if not isinstance(payload, list):
        return []
    return list(payload)


def _extract_json(text: str) -> str | None:
    """从自由文本里取出第一个完整的 JSON 数组/对象。

    手写扫描而不是正则：模型常把数组包在 ```json 围栏或一句解释里，而正则切不出
    配对的括号，会把嵌套对象的尾部截掉。
    """
    start = -1
    opening = ""
    depth = 0
    in_string = False
    escaped = False
    for position, char in enumerate(text):
        if start < 0:
            if char in "[{":
                start = position
                opening = char
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                closing = "]" if opening == "[" else "}"
                if char != closing:
                    return None
                return text[start : position + 1]
    return None


def _to_subtask(item: object, *, index: int) -> Subtask:
    """一条原始计划项 → ``Subtask``。缺指令或缺上下文都判读不出来。"""
    if not isinstance(item, dict):
        raise PlanParseError(f"子任务 {index} 不是 JSON 对象")
    instruction = str(
        item.get("instruction") or item.get("task") or item.get("step") or ""
    ).strip()
    context = str(item.get("context") or item.get("background") or "").strip()
    if not instruction:
        raise PlanParseError(f"子任务 {index} 缺少 instruction")
    if not context:
        raise PlanParseError(f"子任务 {index} 缺少 context")
    return Subtask(
        index=index,
        instruction=instruction,
        context=context,
        depends_on=_dependencies(item.get("depends_on")),
    )


def _dependencies(raw: object) -> tuple[int, ...]:
    """planner 给的依赖下标：只留非负整数、去重保序。

    L2 只有一个 worker、串行执行，依赖暂时没有可执行的含义；这里解析是为了不在读计划
    时把它悄悄丢掉——L3 要靠它做定向重跑。
    """
    return nonnegative_indices(raw)


def render_plan(
    plan: Plan,
    *,
    results: tuple[tuple[str, str], ...] = (),
) -> str:
    """把计划（及其已有产出）渲染成人读文本。

    ``results`` 与子任务按下标一一对应；``("", "")`` 表示这一格还没跑。进化成
    JSON 之前先有这一段人读形状——planner 与 Critic 都读它，测试也断言它。
    """
    lines: list[str] = []
    for subtask in plan.subtasks:
        lines.append(f"子任务 {subtask.index}：{subtask.instruction}")
        if subtask.context:
            lines.append(f"  背景：{subtask.context}")
        if subtask.depends_on:
            lines.append(f"  依赖：{list(subtask.depends_on)}")
        if subtask.index < len(results):
            output, trace = results[subtask.index]
            if output:
                lines.append(f"  产出：{output}")
            if trace:
                lines.append(f"  轨迹：{trace}")
            if not output and not trace:
                lines.append("  产出：（尚未执行）")
    return "\n".join(lines)


def build_plan_message(*, goal: str, max_subtasks: int) -> str:
    """planner 的输入：目标 + 输出形状。刻意短——分解不需要探索工作区。"""
    return (
        "把下面的编码目标拆成按顺序执行的子任务，只回一个 JSON 数组，"
        "不要回 JSON 以外的任何内容。\n\n"
        f"目标：{goal}\n\n"
        f"子任务数量不超过 {max_subtasks} 个。每个元素形如：\n"
        '{"instruction": "这一步具体做什么", '
        '"context": "执行这一步需要的前置事实（失败长什么样、根因、涉及哪个文件哪一行）", '
        '"depends_on": [前置子任务下标]}\n\n'
        "`instruction` 与 `context` 都是必填；`context` 要写到执行者**不必回头问**"
        "的程度：它会先读这里的背景，再动手。"
    )


def build_plan_review_message(
    *,
    goal: str,
    plan: Plan,
    results: tuple[tuple[str, str], ...],
) -> str:
    """Critic 的输入：目标 + 计划 + 每个子任务的产出与轨迹。**不含判决。**"""
    return (
        "下面是多 agent 的一轮计划与执行结果。请判断各子任务是否真的推进了目标"
        "（可以自己用工具复核），然后用下面的格式给出终局。\n\n"
        f"目标：{goal}\n\n"
        "计划与结果：\n"
        f"{render_plan(plan, results=results)}\n\n"
        "输出格式（二选一，必须严格以此开头）：\n"
        f"* 认可：`{APPROVE}`\n"
        "* 打回：只回一个 JSON 对象，形如 "
        '{"verdict": "revise", "instructions": "下一步具体改什么，落到文件/行/命令上", '
        '"targets": [需要重跑的子任务下标]}\n\n'
        f"`targets` 为空表示整条计划重跑；不用 JSON 的旧式 `{REVISE}: ...` 仍可读，"
        "但无法定向重跑。\n"
        "打回时给出可执行的下一步；不要猜判决。"
    )


__all__ = [
    "Plan",
    "PlanParseError",
    "Subtask",
    "build_plan_message",
    "build_plan_review_message",
    "parse_plan",
    "render_plan",
]
