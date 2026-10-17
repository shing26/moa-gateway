"""L0：§5.0 的单 agent 循环跑一份。基线档，没有回边也没有扇出。

L0 存在的意义是给 L1–L3 当对照：它跑的是与四档完全相同的工具、指令、预算与判停
条件，唯一"缺"的是编排拓扑。所以这里只是把 ``LLMBackedAgent`` 包一层注册进去，
不额外塑形。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.tools import ToolRegistry

from bench.agent.loop import LLMBackedAgent
from bench.agent.types import CodingAgent
from bench.arms.registry import Arm


def build_l0(*, llm: Any, workspace: Path, tools: ToolRegistry) -> CodingAgent:
    # workspace 是注册表给四档留的统一构造形状；L0 的它由 run() 接收，构造期不用。
    return LLMBackedAgent(llm, tools)


L0 = Arm(
    name="L0",
    description="单体 ReAct 循环（§5.0 的 agent 跑一份），协作剂量的零点",
    build=build_l0,
)

__all__ = ["L0", "build_l0"]
