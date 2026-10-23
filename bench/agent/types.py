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
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from app.agents.tool_contract import OutcomeKind

#: agent 的自报状态。"fail" 刻意不在其中——那是判决，见模块 docstring。
AgentStatus = Literal["completed", "step_limit", "budget_exhausted", "error"]

#: harness 的判决。"pass" / "fail" 来自验证命令；"budget_exhausted" / "error" 的意思是
#: **没有判决可下**——由执行体自报、harness 原样转记，不是"判决为不达标"。
Outcome = Literal["pass", "fail", "budget_exhausted", "error"]

#: 预算档位（§5.3）。``B1`` = 等预算主实验（四档共用同一个美元上限）；``B3`` =
#: 只对 B1 里失败的档位放宽上限的复跑。定义在 types 而不是 harness/budget.py，是因为
#: ``RunResult`` 要把档位写进落盘物——放另一边会绕回一个 import 环。字面量类型也意味着
#: 落盘校验、CLI choices 与报告三处读的是同一个来源，不再各自写裸字符串。
BudgetTier = Literal["B1", "B3"]

B1_TIER: BudgetTier = "B1"
B3_TIER: BudgetTier = "B3"

#: 等预算主轴（§5.3）。``cost`` = 四档共用同一个美元上限（默认，provider 有定价时）；
#: ``tokens`` = 四档共用同一个 token 上限（provider 不给成本信号时——如免费端点——美元
#: 恒为 0，"同等花费"无从比较，token 是唯一可比的货币）。闭集字面量与报告、CLI 读同一
#: 个来源，避免各自写裸字符串。
BudgetAxis = Literal["cost", "tokens"]

COST_AXIS: BudgetAxis = "cost"
TOKEN_AXIS: BudgetAxis = "tokens"

#: 任务来源。``E1`` = 真实历史 bug（修复来自 commit），``E3`` = 合成缺陷（注入自
#: ``test.patch``）。报告按它把两类任务分开说，所以是闭集字面量而不是裸字符串——
#: 与 ``AgentStatus`` / ``Outcome`` / ``BudgetTier`` 同一套写法，落盘物校验与报告
#: 读的是同一个来源。
TaskOrigin = Literal["E1", "E3"]

E1_ORIGIN: TaskOrigin = "E1"
E3_ORIGIN: TaskOrigin = "E3"


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
class LLMRetryEvent:
    """一次失败的真实 LLM 尝试；用于区分“最终成功”与“贯穿 429 后成功”。"""

    call_index: int
    failed_attempt: int
    max_attempts: int
    delay_ms: int
    error_type: str
    status_code: int | None = None
    rate_limited: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Usage:
    """一次 run 的账本。来源是 ``LLMClient.last_metrics``（N3 已建好的通道）。"""

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0
    #: 每次真实 LLM 请求的稳定摘要（messages + tool schemas），用于复核实验
    #: 时确认输入上下文没有在静默变化。
    prompt_hashes: tuple[str, ...] = ()
    #: 每次调用的 provider 停止原因。``length`` 必须在账本里可见，否则空输出
    #: 只能被解释成"模型没想好"，无法区分是输出额度问题。
    finish_reasons: tuple[str, ...] = ()
    #: reasoning 总字符数。只记规模，不把思维链正文写进 Run Record。
    reasoning_chars: int = 0
    #: 被有界重试吸收的调用故障。没有它，一次先 429 后成功的 run 看起来与
    #: 全程稳定成功的 run 完全相同，容量门禁就无法复核。
    retry_events: tuple[LLMRetryEvent, ...] = ()

    @property
    def total_tokens(self) -> int:
        """进出的 token 总量——token 兜底上限与报告都按这个数看。"""
        return self.prompt_tokens + self.completion_tokens

    def add_call(self, metrics: Mapping[str, Any] | None) -> "Usage":
        """并入一次 LLM 调用的用量。``llm_calls`` 每次 +1，与 metrics 是否为空无关。"""
        values = metrics or {}
        prompt_hash = str(values.get("prompt_hash", "") or "")
        finish_reason = str(values.get("finish_reason", "") or "")
        return Usage(
            llm_calls=self.llm_calls + 1,
            prompt_tokens=self.prompt_tokens + int(values.get("prompt_tokens", 0) or 0),
            completion_tokens=self.completion_tokens
            + int(values.get("completion_tokens", 0) or 0),
            cost_usd=self.cost_usd + float(values.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=self.llm_latency_ms
            + float(values.get("llm_latency_ms", 0.0) or 0.0),
            prompt_hashes=(
                self.prompt_hashes + (prompt_hash,) if prompt_hash else self.prompt_hashes
            ),
            finish_reasons=(
                self.finish_reasons + (finish_reason,)
                if finish_reason
                else self.finish_reasons
            ),
            reasoning_chars=self.reasoning_chars
            + int(values.get("reasoning_chars", 0) or 0),
            retry_events=self.retry_events,
        )

    def add_retry(self, event: LLMRetryEvent) -> "Usage":
        return replace(self, retry_events=(*self.retry_events, event))

    def __add__(self, other: "Usage") -> "Usage":
        """把多段执行体的账并进同一份。"""
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            llm_calls=self.llm_calls + other.llm_calls,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            llm_latency_ms=self.llm_latency_ms + other.llm_latency_ms,
            prompt_hashes=self.prompt_hashes + other.prompt_hashes,
            finish_reasons=self.finish_reasons + other.finish_reasons,
            reasoning_chars=self.reasoning_chars + other.reasoning_chars,
            retry_events=self.retry_events + other.retry_events,
        )


@dataclass(frozen=True)
class Budget:
    """两条互相独立的硬上限。

    ``max_cost_usd`` 是主判据（§5.3：等预算对比按美元算）。它只能**事后**累计——
    调用前无从得知这次花多少——所以实现上允许最后一次调用把预算越过一点，判据是
    "累计值已达到上限"。``max_steps`` 是兜底，防止不花钱的原地打转。

    ``max_tokens`` 是 §5.3 的第二条兜底：防止单个 agent 无节制地刷 token。它**不做
    主线判据**（主线判美元），``0`` 表示不设。命中它同样归 ``budget_exhausted``——
    与"想继续但没额度了"是同一件事。
    """

    max_cost_usd: float
    max_steps: int
    max_tokens: int = 0

    def remaining(self, *, usage: Usage, steps_used: int) -> "Budget":
        """从全局累计用量里切出下一阶段可用的预算。"""
        return Budget(
            max_cost_usd=max(0.0, self.max_cost_usd - usage.cost_usd),
            max_steps=max(0, self.max_steps - steps_used),
            max_tokens=self.max_tokens,
        )

    def stop_status(self, *, usage: Usage, steps_used: int) -> AgentStatus | None:
        """全局预算的判停顺序：成本先于 token 兜底、token 先于步数；都未到返回 ``None``。"""
        if usage.cost_usd >= self.max_cost_usd:
            return "budget_exhausted"
        if self.max_tokens > 0 and usage.total_tokens >= self.max_tokens:
            return "budget_exhausted"
        if steps_used >= self.max_steps:
            return "step_limit"
        return None


@dataclass(frozen=True)
class SubtaskUsage:
    """并发档里单个 worker 的账本切片，用来证明用量可以归因。"""

    index: int
    usage: Usage
    turns: int
    status: AgentStatus
    model: str = ""


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
    #: 每个子任务的账本切片。串行档可以留空；并发档必须填，否则共享的
    #: ``Usage`` 只能说明总成本，不能说明是谁花的。
    subtask_usage: tuple[SubtaskUsage, ...] = ()
    #: 并行 worker 的写集摘要。正文只留在内部合并过程中，不进入 Run Record。
    workspace_writes: tuple[dict[str, Any], ...] = ()
    #: 同一路径的分歧写入。冲突时目标文件保留合并前内容，候选摘要留在这里。
    workspace_conflicts: tuple[dict[str, Any], ...] = ()
    #: 已确定合并到主工作区的文件级变更。
    workspace_merges: tuple[dict[str, Any], ...] = ()
    #: planner 实际产出的结构化计划。L0/L1 为空。
    plan: tuple[dict[str, Any], ...] = ()

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
    #: 这次 run 属于哪一档预算（§5.3）。``B1`` = 等预算主实验（四档同一美元上限）；
    #: ``B3`` = 只对 B1 里失败的档位放宽上限的复跑。报告靠它把两档分开。
    budget_tier: BudgetTier = B1_TIER
    #: 同 (task, arm) 在 B1 的花费；只在 B3 复跑时有意义。增量成本 = 本次 - 它。
    baseline_cost_usd: float = 0.0
    #: 同 (task, arm) 在 B1 的 token 用量；只在 B3 复跑时有意义。主轴是 token 时，
    #: 增量按它算——"多花了多少"在免费端点上只能是 token 口径。
    baseline_tokens: int = 0
    #: 任务来源（``E1`` = 真实历史 bug / ``E3`` = 合成缺陷）。从任务定义透传到判决，
    #: 报告靠它把两类任务分开说；缺省按 ``E1`` 读（既有任务全是 E1）。
    source: TaskOrigin = E1_ORIGIN

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def incremental_cost_usd(self) -> float:
        """相对 B1 基线的增量成本（§5.3 要求诚实标注"多花了多少"）。"""
        return max(0.0, self.agent_run.usage.cost_usd - self.baseline_cost_usd)

    @property
    def incremental_tokens(self) -> int:
        """相对 B1 基线的增量 token（主轴是 token 时"多花了多少"的口径）。"""
        return max(0, self.agent_run.usage.total_tokens - self.baseline_tokens)


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
    "B1_TIER",
    "B3_TIER",
    "Budget",
    "BudgetAxis",
    "BudgetTier",
    "COST_AXIS",
    "CodingAgent",
    "E1_ORIGIN",
    "E3_ORIGIN",
    "LLMRetryEvent",
    "Outcome",
    "RunResult",
    "Step",
    "SubtaskUsage",
    "TaskOrigin",
    "TOKEN_AXIS",
    "Usage",
]
