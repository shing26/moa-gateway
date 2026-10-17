"""§5.0 的 agent 原语：类型定义。

两个概念、两个类型
------------------
* ``AgentRun`` —— 执行体的**自我报告**：我为什么停下来。
* ``RunResult`` —— 裁判的**判决**：这次到底算不算过。

之所以必须分开：§7 的全部防作弊设计都建立在"agent 不能自己下判决"上。若把两者
挤进同一个类型，审计时说不清 ``passed: true`` 是谁说的，而"自报完成"与"达成目标"
本来就是两个问题。

``fail`` 不在 ``AgentStatus`` 里
--------------------------------
§8 要求区分失败与噪音，但那三种里有两种归属不同：``fail``（验证没通过）是**判决**，
只能由 harness 在跑完验证后填；``budget_exhausted`` / ``error`` 是**自我报告**
（我为什么停）。所以 ``fail`` 只出现在 ``Outcome``，不出现 ``AgentStatus``。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from app.agents.tool_contract import OutcomeKind

#: agent 的自报状态。"fail" 刻意不在其中——那是判决，见模块 docstring。
AgentStatus = Literal["completed", "step_limit", "budget_exhausted", "error"]

#: harness 的判决。"pass" / "fail" 来自验证命令；"budget_exhausted" / "error" 的意思是
#: **没有判决可下**——由执行体自报、harness 原样转记，不是"判决为不达标"。
Outcome = Literal["pass", "fail", "budget_exhausted", "error"]


@dataclass(frozen=True)
class Step:
    """行动轨迹上的一步。

    ``observation`` 是**回给模型并被截断后**的那一段，不是工具的完整输出——
    轨迹要能解释模型当时看到了什么。
    """

    index: int
    action: Literal["call_tool", "finish"]
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    #: 工具调用的结局；"" 表示这一步是 finish，压根没有工具结局。
    outcome: OutcomeKind | Literal[""] = ""
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class Usage:
    """一次 run 的账本。来源是 ``LLMClient.last_metrics``（N3 已建好的通道）。"""

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0

    def add_call(self, metrics: Mapping[str, Any] | None) -> "Usage":
        """并入一次 LLM 调用的用量。``llm_calls`` 每次 +1，与 metrics 是否为空无关。"""
        values = metrics or {}
        return Usage(
            llm_calls=self.llm_calls + 1,
            prompt_tokens=self.prompt_tokens + int(values.get("prompt_tokens", 0) or 0),
            completion_tokens=self.completion_tokens
            + int(values.get("completion_tokens", 0) or 0),
            cost_usd=self.cost_usd + float(values.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=self.llm_latency_ms
            + float(values.get("llm_latency_ms", 0.0) or 0.0),
        )


@dataclass(frozen=True)
class Budget:
    """两条互相独立的硬上限。

    ``max_cost_usd`` 是主判据（§5.3：等预算对比按美元算）。它只能**事后**累计——
    调用前无从得知这次花多少——所以实现上允许最后一次调用把预算越过一点，判据是
    "累计值已达到上限"。``max_steps`` 是兜底，防止不花钱的原地打转。
    """

    max_cost_usd: float
    max_steps: int


@dataclass(frozen=True)
class AgentRun:
    """agent 的自我报告。**不含判决。**"""

    status: AgentStatus
    steps: tuple[Step, ...]
    usage: Usage
    model: str
    temperature: float
    #: 产出这次 run 的专家（§5.1）。单 agent 档（L0）留空——那里没有角色分工。
    #: 有它，多专家档的用量才**结构上**可归因，而不是靠调用点的约定去记谁是谁。
    expert: str = ""
    stop_reason: str = ""
    error: str = ""
    #: Critic 实际执行的回边轮数（§6 的 ``revise_rounds``）。L0 恒为 0；L1 用它
    #: 把"协调开销"记在结构上，而不是让报告层去数轨迹里的工具调用。
    review_rounds: int = 0
    #: 这一档实际分解出的子任务数（§6 的 ``subtask_count``）。没有分解的档位为 0，
    #: 报告层因此不必去猜轨迹里的子任务边界。
    subtask_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunResult:
    """harness 的判决。**patch 归这里**——它由 harness 从工作区采集，不由 agent 提供。"""

    task_id: str
    arm: str
    base_commit: str
    agent_run: AgentRun
    outcome: Outcome
    patch: str
    fail_first_exit_code: int
    verdict_exit_code: int
    verdict_output: str
    wall_clock_s: float
    #: 采集到的 patch 是否碰过测试文件。§7.5 要求"diff 全留"以便事后核查它是真修了
    #: 还是绕过了；这个布尔是那个核查的第一个信号（判据仍然是 harness 还原后的复验）。
    patch_touched_tests: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CodingAgent(Protocol):
    """L0–L3 各档与单 agent 共用同一签名，判决一律由 harness 组装。

    工具集在**构造期**注入（``run_tests`` 需要任务定义的基底命令），因此这里不带
    工具参数，只带 workspace、目标与预算。
    """

    async def run(self, *, workspace: Path, goal: str, budget: Budget) -> AgentRun: ...


#: runner 取 agent 的方式：``agent_factory(workspace=..., tools=...)``。四档共用这一个
#: 形状，差别只在返回的编排拓扑。定义在这里是因为生产方（``bench/arms``）与消费方
#: （``bench/harness/runner``）都得引用同一份，而不是各自维持一条同形的别名。
AgentFactory = Callable[..., CodingAgent]


__all__ = [
    "AgentRun",
    "AgentStatus",
    "AgentFactory",
    "Budget",
    "CodingAgent",
    "Outcome",
    "RunResult",
    "Step",
    "Usage",
]
