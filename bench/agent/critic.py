"""L1 的 Critic（§5 四档表）：判据是**轨迹**，不是仓库里的 diff。

为什么 Critic 看不到 patch
--------------------------
``CodingAgent.run(workspace, goal, budget) -> AgentRun`` 是冻结的签名（ADR-023），
而被采集的 patch 与 ``outcome`` 都由 harness 在跑完之后产出。所以"看它的 patch 与
验证输出"在冻结接口下拿不到字面意义上的 diff；Critic 可用的输入只有 agent 的
``steps``——工具调用加截断后的观察。它要看文件就自己用工具重读，要看验证结果就自己
``run_tests``。

判据因此是轨迹加**它自己复核到的事实**，不是 harness 的判决。它拿不到
``verdict_exit_code``，也不该猜——"自报完成"不等于"通过"（CONTEXT.md 的「执行体
与判定」），真正的判决仍归 harness。

两种思考、两条路：这里的 ``parse_verdict`` 只做**保守**的解析
------------------------------------------------------------
Critic 的终局必须落成一个二值信号加一句可执行的下一步。解析器只认两种形状：

* ``APPROVED`` 字面量，或带 ``verdict: approve`` 的 JSON；
* ``REVISE: <指令>`` 字面量，或带 ``verdict: revise`` 的 JSON。

其余一律按"打回"处理——点评读不出来时替 agent 说"通过"，正是 ADR-023 背景里批评
的"永远不会失败的执行体"换了个地方复发。原文留下当指令，至少不丢信息。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bench.agent.parsing import nonnegative_indices
from bench.agent.types import Step

#: 通过的字面量。大小写不敏感。
APPROVE = "APPROVED"
#: 打回的前缀的规范写法，后接可执行指令。
REVISE = "REVISE"

#: 注入下一轮上下文时的标记。测试与审计都靠它认出"这条是 Critic 的指令"。
REVISE_INSTRUCTION_PREFIX = "【Critic 修正指令】"

#: 轨迹渲染给 Critic 看的字符上限。轨迹本身已经是截断过的观察，但一轮次多的
#: run 仍可能撑爆上下文——超了就从中间省略，去掉的是最没信息量的重复尝试。
TRAJECTORY_LIMIT = 30_000


@dataclass(frozen=True)
class ReviewPolicy:
    """回边的硬上限与指令长度上限——两条都是"别再转下去"的闸门。"""

    max_rounds: int = 2
    max_instructions_chars: int = 4_000


@dataclass(frozen=True)
class Critique:
    """Critic 对一轮产出的裁定。``approved`` 为假时 ``instructions`` 必有内容。"""

    approved: bool
    instructions: str = ""
    #: L2/L3 的计划级评审可以点名需要重跑的子任务；单 agent 的 L1 留空。
    targets: tuple[int, ...] = ()


_JSON_BLOCK = re.compile(r"\{.*?\}", re.DOTALL)


def parse_verdict(text: str, *, instructions_limit: int = 4_000) -> Critique:
    """把 Critic 的终局解析成 ``Critique``。读不出来时**保守判打回**。"""
    raw = (text or "").strip()
    if not raw:
        return Critique(approved=False, instructions="Critic 没有给出可执行的下一步。")

    for blob in _JSON_BLOCK.findall(raw):
        try:
            payload = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        verdict = str(payload.get("verdict", "")).strip().lower()
        if verdict in ("approve", "approved", "pass"):
            return Critique(approved=True)
        if verdict in ("revise", "revision", "reject", "rejected"):
            return Critique(
                approved=False,
                instructions=_clip(
                    str(payload.get("instructions") or payload.get("reason") or raw),
                    instructions_limit,
                ),
                targets=nonnegative_indices(payload.get("targets")),
            )

    if raw.upper().startswith(APPROVE):
        return Critique(approved=True)
    if raw.upper().startswith(REVISE):
        body = raw[len(REVISE) :].lstrip("：: -—\t").strip()
        return Critique(
            approved=False,
            instructions=_clip(body or raw, instructions_limit),
        )
    # 既不 approve 也没有结构化 verdict：保守判打回，原文留下当指令。
    return Critique(approved=False, instructions=_clip(raw, instructions_limit))


def render_trajectory(steps: tuple[Step, ...], *, limit: int = TRAJECTORY_LIMIT) -> str:
    """把 agent 的轨迹压成 Critic 能读的一段文本。

    每条观察已经是回给模型并被截断过的版本（``Step.observation``），这里只负责
    加"哪一步调了什么、看到了什么"的骨架，以及整体字节上限。
    """
    lines: list[str] = []
    for position, step in enumerate(steps, start=1):
        if step.action == "finish":
            lines.append(f"{position}. [结束] {step.observation}")
            continue
        outcome = step.outcome or "ok"
        lines.append(f"{position}. 调用 {step.tool}（{outcome}）")
        if step.observation:
            lines.append(f"   观察：{step.observation}")
    body = "\n".join(lines) if lines else "（这一轮没有工具调用，agent 直接声明完成）"
    return _clip(body, limit)


def build_review_message(*, goal: str, steps: tuple[Step, ...]) -> str:
    """Critic 的输入：目标 + 轨迹 + 输出格式。**不含 patch，也不含判决。**"""
    return (
        "下面是单 agent 的一轮行动轨迹。请判断它是否真的达成了目标。\n"
        "轨迹里的观察是被截断过的，如果不确定，可以自己用工具复核"
        "（读文件、跑 run_tests），但复核完必须用下面的格式给出终局"
        "（终局就一句 verdict，不要夹带解释）。\n\n"
        f"目标：{goal}\n\n"
        "行动轨迹：\n"
        f"{render_trajectory(steps)}\n\n"
        "输出格式（二选一，必须严格以此开头）：\n"
        f"* 认可：`{APPROVE}`\n"
        f"* 打回：`{REVISE}: <下一步具体改什么，落到文件/行/命令上>`\n\n"
        "打回时给出的下一步要**可执行**（「再检查一下」不是指令）；"
        "不确定时自己也别猜判决——只指出轨迹里没做到目标的地方。"
    )


def format_revise_instruction(instructions: str) -> str:
    """把 Critic 的打回转成注入 worker 下一轮上下文的用户消息。"""
    return (
        f"{REVISE_INSTRUCTION_PREFIX}上一轮被打回了，请接着上一轮的历史继续修改，"
        "不要从头重做。\n"
        f"{instructions.strip()}\n"
        "改完用 run_tests 验证，然后结束本轮。"
    )


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n…（中略 {len(text) - limit} 字符）…\n{text[-tail:]}"


__all__ = [
    "APPROVE",
    "REVISE",
    "REVISE_INSTRUCTION_PREFIX",
    "Critique",
    "ReviewPolicy",
    "build_review_message",
    "format_revise_instruction",
    "parse_verdict",
    "render_trajectory",
]
