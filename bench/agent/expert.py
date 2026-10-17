"""§5.1 的专家：与 L0 共用同一条工具循环，只多一个角色身份。

先查清"现有工具循环能不能复用"，再决定写什么
--------------------------------------------
设计 §5.1 要求实现前先查 ``app/agent_core/task_agent.py`` 一带有没有可复用的工具
循环。仓库里实测有三处，结论是**复用循环机制，但不复用这三份实现**：

* ``app/agents/stubs.py`` 的 ``_execute_with_tools``：coder / general 现在走的就是
  它，但出口是 ``execute() -> str``——"完成"与"失败"共用一个字符串，正是 ADR-023
  背景里说的"永远不会失败的执行体"。
* ``app/agent_core/react.py`` 的 ``ReActLoop``（``task_agent.py`` 在用）：决策协议是
  **JSON-in-text**。ADR-023 决策 3 已经把在线动作协议唯一化为原生 tool-calling，
  再套这条就是重新引入 ADR-018 要消灭的第二套协议。
* ``bench/agent/loop.py`` 的 ``LLMBackedAgent``（§5.0 落地）：原生 tool-calling，
  同一份 ``call_bench_tool`` 契约，产出 ``AgentRun``（轨迹 + 用量）。

所以专家不是新写一个循环，而是**把角色身份接到 ``LLMBackedAgent`` 上**：工具集、
工具契约、记忆策略、停止条件、账本形状全部沿用，差别只在 system prompt 里多一段
"你是谁"，以及产出上多一个 ``AgentRun.expert``。

为什么专家落在 ``bench/``，而不是改 ``app/orchestration/collaboration.py``
--------------------------------------------------------------------------
§5.1 点名的是协作链路里的 coder / general / review，但那条链路有两个属性与本实验
相冲：

1. 它的专家是 ``SubAgent``（``execute() -> str``），没有"失败"这个结局；
2. 它整条图带着 guard / evaluator / HITL / 审计，而 §4.1 明确要求实验台绕开这些
   ——"变量要干净"，那些组件与编排结构没有因果关系，只会给对比加噪音。

再加上 ADR-023 决策 1（原语落 ``bench/``，``app/`` 零改动），落点只剩一个：**在
``bench/`` 内做一套带工具的专家**。代价是短期内专家有两份实现（``app/`` 的聊天型 +
``bench/`` 的行动型），这笔债与"两套工具循环"记在同一处（ADR-023 后果）。

专家的词汇（coder / general / review）与协作链路的 ``EXPERT_KEYS`` 一致——同一个
概念不另起一套名字；但这里**不 import** 那份常量，``app/orchestration`` 会拖进
langgraph 与整条治理链，那是实验台刻意不要的。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app.agents.tools import ToolRegistry

from bench.agent.loop import LLMBackedAgent, LoopSession
from bench.agent.memory import MemoryPolicy
from bench.agent.types import AgentRun, Budget

@dataclass(frozen=True)
class ExpertSpec:
    """一个专家的静态定义：名字、一句话分工，以及它的角色指令。"""

    key: str
    description: str
    instructions: str


#: "哪些专家可用"的单一来源。加一个角色 = 在这里加一条并同步 ``EXPERT_KEYS``。
EXPERTS: Mapping[str, ExpertSpec] = {
    "coder": ExpertSpec(
        key="coder",
        description="写代码、改代码、修 bug 的专家",
        instructions=(
            "你的角色是编码专家。改动前先读相关文件与失败输出，定位到根因再动手；"
            "只做让目标通过所需的最小改动，改完用 run_tests 验证；"
            "测试仍红就根据新输出换思路，不要重复同一个已经失败过的改法。"
        ),
    ),
    "general": ExpertSpec(
        key="general",
        description="通用推理与文档类工作的专家",
        instructions=(
            "你的角色是通用专家。先把问题拆清楚再动手，结论要能落到具体文件或具体命令上；"
            "需要动代码时按最小改动来，并用 run_tests 验证。"
        ),
    ),
    "review": ExpertSpec(
        key="review",
        description="审查代码、核对改动是否真的解决问题的专家",
        instructions=(
            "你的角色是审查专家。先读代码与改动本身，再判断它是否真的解决了目标；"
            "发现问题就直接改成可用的版本，并用 run_tests 验证，不要只给一句评价。"
        ),
    ),
}

#: 专家池的词汇。与 ``app/orchestration/collaboration.py`` 的 ``EXPERT_KEYS`` 同名同序，
#: 但那边是"聊天型专家"，这边是"行动型专家"——见模块 docstring。**从 ``EXPERTS``
#: 派生**，不另抄一份字面量：可用清单只有一处，加角色不会漏改另一半。
EXPERT_KEYS: tuple[str, ...] = tuple(EXPERTS)


class UnknownExpertError(LookupError):
    """请求了专家池里没有的角色。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"未知专家: {key!r}")
        self.key = key


class ToolExpert:
    """一个带工具循环的专家（§5.1）。L2/L3 的 worker 就是它。

    与 L0 的差别只有两处：system prompt 里多一段角色指令，产出上多一个
    ``AgentRun.expert``。循环、工具、契约、记忆、预算、停止条件全部继承。
    """

    def __init__(
        self,
        spec: ExpertSpec,
        llm: Any,
        tools: ToolRegistry,
        *,
        policy: MemoryPolicy | None = None,
        model: str = "",
        temperature: float | None = None,
    ) -> None:
        self._spec = spec
        self._agent = LLMBackedAgent(
            llm,
            tools,
            policy=policy,
            model=model,
            temperature=temperature,
            instructions=spec.instructions,
        )

    @property
    def key(self) -> str:
        return self._spec.key

    def session(self, *, workspace: Path, goal: str, budget: Budget) -> LoopSession:
        """开一条可续跑的会话。L1 的 Critic 走这条（同一条循环 + 角色指令）。"""
        return self._agent.session(workspace=workspace, goal=goal, budget=budget)

    async def run(self, *, workspace: Path, goal: str, budget: Budget) -> AgentRun:
        run = await self._agent.run(workspace=workspace, goal=goal, budget=budget)
        # 只加标签，不动别的：轨迹与用量原样来自同一条循环，账本因此是同一条。
        # 一个 ``AgentRun`` 只装得下一个专家名——L3 把 N 个专家的 run 合进一份账本时，
        # 逐专家的用量拆分是那一档自己的事（见 ticket 06），这里只保证每个专家的
        # run 自身可归因。
        return replace(run, expert=self._spec.key)


class ExpertPool:
    """按名字造专家。L2/L3 从这里取 worker，不各自 new 一份。

    工具集在构造期注入（与 L0 同一个 ``ToolRegistry`` 实例），所以池里的专家与
    L0 用的是**同一套工具、同一份契约**——没有第二条工具路径可走。

    池里的专家共用传入的 ``llm`` 实例。L3 并发时每个 worker 需要各自独立的 client
    （``last_metrics`` 是单值通道，共用会串账），那是 L3 自己决定怎么造 client 的事，
    池不替它做主。
    """

    def __init__(
        self,
        *,
        llm: Any,
        tools: ToolRegistry,
        policy: MemoryPolicy | None = None,
        model: str = "",
        temperature: float | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._policy = policy
        self._model = model
        self._temperature = temperature

    def keys(self) -> tuple[str, ...]:
        return EXPERT_KEYS

    @property
    def llm(self) -> Any:
        """池的循环机制的两个依赖。需要"与专家同一套工具"的中性 worker 时才用它们
        （L1 的 worker 就是这样：没有角色，但工具与专家池一致）。"""
        return self._llm

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    def get(self, key: str) -> ToolExpert:
        try:
            spec = EXPERTS[key]
        except KeyError:
            raise UnknownExpertError(key) from None
        return ToolExpert(
            spec,
            self._llm,
            self._tools,
            policy=self._policy,
            model=self._model,
            temperature=self._temperature,
        )


__all__ = [
    "EXPERT_KEYS",
    "EXPERTS",
    "ExpertPool",
    "ExpertSpec",
    "ToolExpert",
    "UnknownExpertError",
]
