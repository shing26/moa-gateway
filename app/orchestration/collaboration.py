"""多 Agent 协作编排（ADR-022）。

Why this file exists
--------------------
R6（多级 supervisor）在 ADR-022 立项前一直处于冻结状态。本模块把它解冻落地：
一条**独立于请求主路径**的协作链路，形态是 Supervisor + 专家 + Critic 反思回边。

与 ``app/orchestration/graph.py``（ADR-008 等价性适配器）的关系
-------------------------------------------------------------
两者是**不同的东西**，不共用状态、不改同一张图：

* ADR-008 的 ``LangGraphOrchestrator`` 把**单 Agent 请求路径**表达成图，用来证明
  图运行时与 FSM 管道等价。它是"同一条路径的第二种写法"。
* 本模块提供的是 FSM 路径**没有的能力**：多 Agent 分工与反思。它是"一条新路径"。

两者复用的是**同一批治理原语**——``execute_with_retry``、``RuleEvaluator``、
``_merge_guard``、``_verdict_from_eval_issues``、guard/HITL、审计 WAL。治理只有
一份定义，所以协作链路**不会**因为"多 Agent"而绕过守卫，也不会让守卫语义分叉。

治理之下的协作（这是差异化叙事）
--------------------------------
"多 Agent"本身不稀奇，稀奇的是每个专家的输出仍然要过同一条守卫链：评估器 →
三级 guard 合并 → HITL → 审计。Critic 打回只重跑被打回的子任务，不是全链重跑；
达到轮次上限仍不通过时标 ``need_human_review`` 走人工，而不是把没通过的输出
伪装成成功交付。

关键约束（ADR-022）
------------------
* 本模块**不进入** ``INTENT_AGENT_MAP`` / 意图路由。它是显式入口
  （``POST /api/v1/collab`` 与 CLI），不是请求主路径的一站。
* 不改 ``MoAPipeline``、不改 ``app/orchestration/graph.py``，因此
  ``tests/unit/test_langgraph_adapter.py`` 的协作者表面漂移门禁保持绿色。
* 默认关闭：``COLLAB_ENABLED=false`` 时本模块不被请求路径引用，行为零变化。
"""

from __future__ import annotations

import asyncio
import logging
import operator
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

import app.agents.loader  # noqa: F401  (import for agent registration side effects)
from app.agents.contract import AgentEnvelope, get_agent
from app.agents.retry import AgentExecutionFailed, execute_with_retry
from app.audit.models import AuditEntry
from app.audit.recorder import record
from app.config import settings
from app.evaluator.evaluator import RuleEvaluator
from app.guard.guard_service import GuardianAction, GuardVerdict, guard_service
from app.guard.rbac import Role
from app.memory import ConversationMemory
from app.middleware.request_logger import bind_trace
from app.models.events import new_trace_id
from app.models.errors import ErrorCode
from app.outbound.adapter import ResponseAdapter
from app.pipeline import (
    FAILURE_ESCALATION_TEXT,
    _merge_guard,
    _tool_failure_issues,
    _verdict_from_eval_issues,
)

logger = logging.getLogger("moa.orchestration.collaboration")

# 协作链路的意图标签。意图路由不认识它（**刻意如此**，见模块 docstring），
# 评估器与 guard 因此按"通用输出"的规则对待整份协作结果。
COLLAB_INTENT = "collaboration"
# 聚合审计条目的 agent_name。与子任务条目（写真实专家名）区分开，
# 于是"这条是子任务痕迹"和"这条是一次协作的总结论"在审计里一眼可分。
COLLAB_AGENT_NAME = "collaboration"
# 审计里 agent_output 的截断长度：多份专家输出拼起来可能很长，WAL 论行计费。
_AUDIT_EXCERPT = 2000

# 这两种 status 表示"这个子任务已经没救了"，图不该再送去评审，直接走升级腿。
_ESCALATED_STATUSES = frozenset({"pending_review", "error"})

# 专家池：Supervisor 只能把子任务派给这些已注册的 Agent。这是"handoff 复用现有
# AGENT_REGISTRY"的具体边界——协作链路不引入新的 Agent 实现。
EXPERT_KEYS: tuple[str, ...] = ("coder", "general", "review")

# Critic 判定合法值。只认这两个，避免 LLM 自由发挥出第三种状态把图带进死路。
DECISION_APPROVE = "approve"
DECISION_REVISE = "revise"

# 输出里出现这些标记时，Mock Critic 判定"需要重做"。真实 Critic 由 LLM 给出理由。
_FAILURE_MARKERS = ("错误", "失败", "error", "Error", "ERROR", "exception", "Traceback")


# ── 公开类型 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubtaskAssignment:
    """Supervisor 派出的一个子任务。"""

    index: int
    instruction: str
    agent: str
    depends_on: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "instruction": self.instruction,
            "agent": self.agent,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class CritiqueVerdict:
    """Critic 对一轮输出的结构化裁决。"""

    decision: str
    reasons: tuple[str, ...] = ()
    targets: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reasons": list(self.reasons),
            "targets": list(self.targets),
        }


@dataclass(frozen=True)
class CollabResult:
    """一次协作运行的最终结果。

    ``status`` 沿用请求路径的词汇，让审计与监控不必为协作单开一套状态机：
    ``ok``（直接交付）/ ``approved``（人工放行后交付）/ ``pending_review``
    （挂在 interrupt 上等审批）/ ``blocked``（guard 拦下）/ ``rejected``
    （人工驳回）/ ``error``（子任务重试耗尽且审批关闭，见 ``error_code``）。
    ``need_human_review`` 在"Critic 未通过但 HITL 关闭"时为真——此时**照样交付**，
    但交付文本里显式标注「critic 未通过」，不伪装成成功。
    """

    trace_id: str
    task: str
    plan: tuple[SubtaskAssignment, ...]
    rounds: int
    per_agent_outputs: dict[str, str]
    critique_history: tuple[CritiqueVerdict, ...]
    final_text: str
    status: str
    need_human_review: bool
    cost_usd: float
    llm_latency_ms: float
    tool_calls: int
    node_path: tuple[str, ...]
    guard_action: str = ""
    error_code: str = ""


# ── 协作 LLM 协议 ────────────────────────────────────────────────────────


class CollaborationLLM(Protocol):
    """协作链路的决策层：规划与评审。"""

    async def plan(self, *, task: str) -> list[dict[str, Any]]:
        """把任务拆成子任务列表。

        返回 dict 列表，键为 ``index`` / ``instruction`` / ``agent`` / ``depends_on``。
        ``agent`` 必须是 ``EXPERT_KEYS`` 里的值——越界即视为规划失败。
        """
        ...

    async def critique(
        self, *, task: str, plan: list[dict[str, Any]], outputs: dict[int, str]
    ) -> dict[str, Any]:
        """对当前输出给出 ``{decision, reasons, targets}``。"""
        ...


_AGENT_HINTS: tuple[tuple[str, str], ...] = (
    ("review", ("审查", "review", "PR", "pull request", "github", "diff", "代码评审")),
    ("coder", ("代码", "函数", "编程", "实现", "写一个", "bug", "重构", "code", "python")),
)


def _pick_agent(instruction: str) -> str:
    """按关键词给子任务选专家。命中不了就归 general。"""
    body = instruction or ""
    for agent, needles in _AGENT_HINTS:
        if any(needle in body for needle in needles):
            return agent
    return "general"


def _to_assignment(item: dict[str, Any]) -> SubtaskAssignment:
    """dict → ``SubtaskAssignment``。checkpoint 里只放 JSON 原生类型。"""
    return SubtaskAssignment(
        index=int(item.get("index", 0) or 0),
        instruction=str(item.get("instruction", "") or ""),
        agent=str(item.get("agent", "general") or "general"),
        depends_on=tuple(
            int(dep)
            for dep in (item.get("depends_on") or ())
            if isinstance(dep, (int, float)) and not isinstance(dep, bool)
        ),
    )


def _assignments(plan: Any) -> tuple[SubtaskAssignment, ...]:
    return tuple(
        _to_assignment(item) for item in (plan or ()) if isinstance(item, dict)
    )


def _to_verdict(item: dict[str, Any]) -> CritiqueVerdict:
    return CritiqueVerdict(
        decision=str(item.get("decision", "") or ""),
        reasons=tuple(str(r) for r in (item.get("reasons") or ())),
        targets=tuple(
            int(t)
            for t in (item.get("targets") or ())
            if isinstance(t, (int, float)) and not isinstance(t, bool)
        ),
    )


def _verdict(history: Any) -> tuple[CritiqueVerdict, ...]:
    return tuple(
        _to_verdict(item) for item in (history or ()) if isinstance(item, dict)
    )


# 规划用的切分符。刻意不含逗号/顿号：那会把一个完整句子拆碎，而中文任务里
# 真正的子任务边界是连接词与断句标点。
_SPLIT_RE = re.compile(r"并且|然后|接着|之后|再|;|；|\n|。")


class MockCollaborationLLM:
    """离线确定性协作 LLM：规则式规划与评审，不依赖外部模型。

    与 ``MockTaskLLM`` 同构：ReAct / handoff / 治理 / 审计全是真实现，只有
    "选哪个专家"与"这轮过不过"由规则模拟。CI 因此能零网络零 token 跑完整协作图。
    """

    def __init__(self, *, max_subtasks: int = 4) -> None:
        self._max_subtasks = max(1, int(max_subtasks))

    async def plan(self, *, task: str) -> list[dict[str, Any]]:
        # 一次性按全部连接词/断句标点切，而不是"找到第一个能切开的就用它"：
        # 后者会把 "写函数，然后审查它，并且总结" 切成两段且第一段仍含着连接词，
        # 于是 _pick_agent 按残留的 "审查" 把整段判给 review——分工就失真了。
        parts = [
            part
            for part in _SPLIT_RE.split(str(task or ""))
            if part and part.strip()
        ]
        cleaned = [
            p.strip().lstrip("。.!！?？;；，,和并").strip()
            for p in parts
            if p and p.strip()
        ]
        cleaned = cleaned or [task]
        # 上限由配置给出（COLLAB_MAX_SUBTASKS），不是这里写死的常数。
        cleaned = cleaned[: self._max_subtasks]
        return [
            {
                "index": i,
                "instruction": text,
                "agent": _pick_agent(text),
                "depends_on": [],
            }
            for i, text in enumerate(cleaned)
        ]

    async def critique(
        self, *, task: str, plan: list[dict[str, Any]], outputs: dict[int, str]
    ) -> dict[str, Any]:
        targets: list[int] = []
        reasons: list[str] = []
        for item in plan:
            index = int(item.get("index", 0))
            text = str(outputs.get(index, "") or "")
            if not text.strip():
                targets.append(index)
                reasons.append(f"子任务 {index} 输出为空")
                continue
            if any(marker in text for marker in _FAILURE_MARKERS):
                targets.append(index)
                reasons.append(f"子任务 {index} 输出含失败标记")
        if targets:
            return {
                "decision": DECISION_REVISE,
                "reasons": tuple(reasons),
                "targets": tuple(targets),
            }
        return {"decision": DECISION_APPROVE, "reasons": (), "targets": ()}


class LiteLLMCollaborationLLM:
    """用已配置的 LLM 做规划与评审。

    遵循 ADR-018：prompt 要求 LLM 只回 JSON，解析失败降级成 Mock 而不是抛。
    协作是**附加能力**，LLM 抽风不该让请求路径震动。
    """

    _PLAN_PROMPT = (
        "你是多 Agent 系统的 Supervisor。把用户任务拆成可并行的子任务，"
        "只回 JSON 数组，每项含 index(从0开始)、instruction、agent、depends_on。"
        "agent 只能是 coder / general / review 之一。不要输出 JSON 以外的内容。"
    )
    _CRITIQUE_PROMPT = (
        "你是多 Agent 系统的 Critic。评审下面各子任务的输出，只回 JSON 对象："
        "{decision: approve|revise, reasons: string[], targets: number[]}。"
        "decision=revise 时 targets 列需要重做的子任务 index。"
    )

    def __init__(self, client: Any = None) -> None:
        if client is None:
            from app.agents.provider import LLMClient, LLMConfig

            client = LLMClient(LLMConfig.from_env("LLM"))
        self._client = client

    async def _complete(self, prompt: str, payload: str) -> str:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": payload},
        ]
        return await self._client.chat(messages)

    @staticmethod
    def _loads(raw: str) -> Any:
        import json

        text = (raw or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        return json.loads(text)

    async def plan(self, *, task: str) -> list[dict[str, Any]]:
        raw = await self._complete(self._PLAN_PROMPT, task)
        try:
            data = self._loads(raw)
        except Exception as exc:  # noqa: BLE001 - 规划失败降级，不让协作链路崩
            logger.warning("collab plan parse failed, falling back to mock: %s", exc)
            return await MockCollaborationLLM().plan(task=task)
        if not isinstance(data, list) or not data:
            return await MockCollaborationLLM().plan(task=task)
        return data

    async def critique(
        self, *, task: str, plan: list[dict[str, Any]], outputs: dict[int, str]
    ) -> dict[str, Any]:
        rendered = "\n".join(
            f"[{item.get('index')}] {item.get('agent')}: {str(outputs.get(int(item.get('index', 0)), ''))[:800]}"
            for item in plan
        )
        raw = await self._complete(self._CRITIQUE_PROMPT, f"任务：{task}\n\n输出：\n{rendered}")
        try:
            data = self._loads(raw)
        except Exception as exc:  # noqa: BLE001 - 同上，评审失败降级
            logger.warning("collab critique parse failed, falling back to mock: %s", exc)
            return await MockCollaborationLLM().critique(
                task=task, plan=plan, outputs=outputs
            )
        if not isinstance(data, dict):
            return await MockCollaborationLLM().critique(task=task, plan=plan, outputs=outputs)
        return data


def build_collaboration_llm(settings_obj: Any = None) -> CollaborationLLM:
    """按 ``COLLAB_LLM`` 选后端。默认 mock（离线可跑）。"""
    cfg = settings_obj or settings
    mode = str(getattr(cfg, "collab_llm", "mock") or "mock").strip().lower()
    if mode in ("litellm", "llm", "live"):
        return LiteLLMCollaborationLLM()
    # 上限取自配置而不是 Mock 的默认常数：否则 COLLAB_MAX_SUBTASKS 调小之后，
    # "规划"这一层还会照样吐出 4 个子任务，只在编排层被截断——审计里看到的
    # 规划结果与真实派发的不一致。
    cap = int(getattr(cfg, "collab_max_subtasks", 4) or 4)
    return MockCollaborationLLM(max_subtasks=cap)
    return MockCollaborationLLM(
        max_subtasks=int(getattr(cfg, "collab_max_subtasks", 4) or 4),
    )


# ── 图状态 ───────────────────────────────────────────────────────────────


def _merge_outputs(left: dict[int, str], right: dict[int, str]) -> dict[int, str]:
    """按子任务序号合并专家输出。

    用 reducer 而不是直接赋值：``Send`` fan-out 出的多个 ``execute_expert``
    在同一个 superstep 里并发返回，谁后写谁赢会丢结果。

    "覆盖同序号旧值"是有意的：critic 打回后重跑子任务 2，``outputs[2]`` 必须
    变成新结果而不是把两轮拼在一起——否则汇总文本里会出现同一子任务的两个版本。
    """
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _add(left: float, right: float) -> float:
    """成本 / 工具计数的 reducer。

    反思回边重跑子任务时那些 token 是**真实花掉的**，所以累加而不是覆盖；
    否则"打回一轮再交付"在审计里的成本会低于实际，预算 guard 也随之失准。
    """
    return float(left or 0.0) + float(right or 0.0)


class CollabState(TypedDict, total=False):
    trace_id: str
    session_id: str
    user_id: str
    task: str
    channel: str
    target: str
    # plan / pending / subtask / verdict / critique_history 存的都是**普通 dict**，
    # 不是上面的 dataclass：checkpointer 会序列化这些值，而 LangGraph 只保证
    # JSON 原生类型的长期可反序列化（自定义类型未来版本会被 block）。
    # dataclass 留在代码里做类型与 to_dict()，跨界时用 _assignments()/_to_verdict() 转。
    plan: tuple[dict[str, Any], ...]
    round: int
    pending: tuple[dict[str, Any], ...]
    # Send 负载：当前 worker 只处理一个子任务。每个 Send 各带一份，
    # 所以多个 worker 之间不会互相覆盖。
    subtask: dict[str, Any]
    outputs: Annotated[dict[int, str], _merge_outputs]
    critique_history: Annotated[list[dict[str, Any]], operator.add]
    verdict: dict[str, Any] | None
    # Critic 轮次耗尽后的标记：照常交付，但交付文本里显式说明"critic 未通过"。
    critique_unresolved: bool
    raw_output: str
    eval_score: float
    eval_issues: tuple[str, ...]
    guard_action: str
    guard_reason: str
    policy_hits: tuple[str, ...]
    hitl_kind: str
    hitl_id: str
    hitl_decision: str
    # 子任务失败原因（多个 worker 可能同时失败，所以是 list 而不是 str）
    failure_reasons: Annotated[list[str], operator.add]
    retry_reason: str
    status: str
    delivered_text: str
    need_human_review: bool
    error_code: str
    cost_usd: Annotated[float, _add]
    llm_latency_ms: Annotated[float, _add]
    tool_calls: Annotated[int, _add]
    tool_errors: Annotated[int, _add]
    tool_arg_rejections: Annotated[int, _add]
    # 已落审计的子任务数（每个子任务一条条目，见 _node_execute_expert）。
    audited_subtasks: Annotated[int, _add]
    # LangGraph 的 operator.add reducer：执行轨迹免费拿到，不用另接 tracer。
    node_path: Annotated[list[str], operator.add]


# ── 离线专家（测试与 demo 用） ───────────────────────────────────────────


class ScriptedExpert:
    """按规则产出确定性文本的专家，不调用任何 LLM。

    存在的理由与 ``MockTaskLLM`` 完全一样：协作图的**拓扑、治理接线、有界终止**
    必须在 CI 里零网络零 token 可验。它**不是**专家能力的替身——真实专家是
    ``AGENT_REGISTRY`` 里的 coder / general / review，由 ``from_deps()`` 装上。

    ``fail_times`` 用来制造"前 N 次调用抛异常"，以便验证重试预算耗尽后的
    升级人工路径——那条路径在 mock 输出永远正常的情况下永远走不到。
    """

    def __init__(self, reply: str | None = None, *, fail_times: int = 0) -> None:
        self._reply = reply
        self._fail_times = max(0, int(fail_times))
        self._calls = 0

    async def execute(self, envelope: AgentEnvelope) -> str:
        self._calls += 1
        if self._calls <= self._fail_times:
            raise RuntimeError(f"scripted expert failure #{self._calls}")
        slot = dict(envelope.agent_local_slot or {})
        agent = str(slot.get("collab_agent", "expert"))
        index = slot.get("collab_subtask_index", 0)
        template = self._reply or (
            # 不含 agent/index 前缀：那是 ``_node_summarize`` 的版面活，
            # 两处都写会让交付文本里同一个标签出现两次。
            "已完成：{instruction}"
        )
        return (
            template
            .replace("{agent}", agent)
            .replace("{index}", str(index))
            .replace("{instruction}", str(envelope.user_raw_input).strip())
        )


# ── 编排器 ──────────────────────────────────────────────────────────────


class CollabOrchestrator:
    """Supervisor + 专家 + Critic 的协作链路（ADR-022 / R6）。

    图结构::

        plan → dispatch →(Send fan-out)→ execute_expert → critic ─┬─ revise ─→ dispatch
                                                                    │   (round+1，有界)
                                                                    ├─ approve → summarize → evaluate → guard ─┬─ allow → deliver
                                                                    │                                            ├─ review → prepare_hitl → await_human ─┬─ approve → deliver
                                                                    │                                            │                                          └─ reject → rejected
                                                                    └─ escalate ────────────────────────────────┘                        └─ deny → blocked

    与 ``LangGraphOrchestrator`` 的分工：那边证明图运行时与 FSM 管道**等价**，
    这边提供 FSM 管道没有的能力。两者共用同一批治理原语，所以"多 Agent"不会
    变成守卫的旁门。

    **刻意不接 FSM**：协作链路不驱动 ``Engine`` 的状态机。原因不是懒——
    ``prepare_hitl`` 若向一个从未 ``MESSAGE_RECEIVED`` 过的会话发
    ``NEEDS_HUMAN``，``INIT + NEEDS_HUMAN`` 是迁移表里没有的一跳；而若协作与
    请求路径共用 session_id，两个写者会去抢同一个会话状态（这正是 per-session
    锁存在的理由）。协作的挂起因此活在 LangGraph checkpoint 里，由 ``resume()``
    放行；审计链完整，审批回路是 ``POST /api/v1/collab/approve``。
    """

    def __init__(
        self,
        *,
        collaboration_llm: Any = None,
        experts: dict[str, Any] | None = None,
        evaluator: Any = None,
        guard_service_obj: Any = None,
        adapter: Any = None,
        memory: Any = None,
        settings_obj: Any = None,
        budget_guard: Any = None,
        checkpointer: Any = None,
    ) -> None:
        cfg = settings_obj or settings
        self._settings = cfg
        self._llm = collaboration_llm or build_collaboration_llm(cfg)
        # 专家可注入：离线测试与 demo 靠它零网络跑完整张图；不给则回到
        # AGENT_REGISTRY，也就是生产的真实专家。
        self._experts: dict[str, Any] = dict(experts or {})
        self._evaluator = evaluator or RuleEvaluator()
        self._guard = guard_service_obj or guard_service
        if adapter is None:
            adapter = ResponseAdapter()
        if memory is None:
            memory = ConversationMemory()
        self._adapter = adapter
        self._memory = memory
        self._budget_guard = budget_guard
        self._max_rounds = max(0, int(getattr(cfg, "collab_max_rounds", 2)))
        self._max_subtasks = max(1, int(getattr(cfg, "collab_max_subtasks", 4)))
        self._checkpointer = checkpointer or InMemorySaver()
        self._graph = self._build()

    # ── construction ───────────────────────────────────────────────────────

    @classmethod
    def from_deps(cls, **kwargs: Any) -> "CollabOrchestrator":
        """Wire against the same singletons the request path uses."""
        from app import deps

        return cls(
            collaboration_llm=build_collaboration_llm(deps.settings),
            evaluator=deps.evaluator,
            guard_service_obj=deps.guard_service,
            adapter=deps.adapter,
            memory=deps.memory,
            settings_obj=deps.settings,
            budget_guard=getattr(deps, "budget_guard", None),
            **kwargs,
        )

    def _build(self) -> Any:
        graph = StateGraph(CollabState)
        graph.add_node("plan", self._node_plan)
        graph.add_node("dispatch", self._node_dispatch)
        graph.add_node("execute_expert", self._node_execute_expert)
        graph.add_node("critic", self._node_critic)
        graph.add_node("summarize", self._node_summarize)
        graph.add_node("evaluate", self._node_evaluate)
        graph.add_node("guard", self._node_guard)
        graph.add_node("prepare_hitl", self._node_prepare_hitl)
        graph.add_node("await_human", self._node_await_human)
        graph.add_node("failed", self._node_failed)
        graph.add_node("deliver", self._node_deliver)
        graph.add_node("blocked", self._node_blocked)
        graph.add_node("rejected", self._node_rejected)

        graph.add_edge(START, "plan")
        graph.add_edge("plan", "dispatch")
        # handoff 就在这里：dispatch 不自己调专家，而是按子任务派 Send。
        # "critic" 也在可达集合里，是 pending 为空时的直通退路（见 _fan_out）。
        graph.add_conditional_edges("dispatch", self._fan_out, ["execute_expert", "critic"])
        graph.add_edge("execute_expert", "critic")
        graph.add_conditional_edges(
            "critic",
            self._after_critic,
            {"revise": "dispatch", "approve": "summarize", "escalate": "prepare_hitl"},
        )
        graph.add_edge("summarize", "evaluate")
        graph.add_edge("evaluate", "guard")
        graph.add_conditional_edges(
            "guard",
            self._after_guard,
            {"allow": "deliver", "review": "prepare_hitl", "deny": "blocked"},
        )
        graph.add_edge("prepare_hitl", "await_human")
        graph.add_conditional_edges(
            "await_human",
            self._after_human,
            {"approve": "deliver", "reject": "rejected", "failed": "failed"},
        )
        graph.add_edge("deliver", END)
        graph.add_edge("failed", END)
        graph.add_edge("blocked", END)
        graph.add_edge("rejected", END)
        return graph.compile(checkpointer=self._checkpointer)

    # ── helpers ───────────────────────────────────────────────────────────

    def _expert(self, key: str) -> Any | None:
        """解析专家：注入的优先，否则回 ``AGENT_REGISTRY``。"""
        if key in self._experts:
            return self._experts[key]
        return get_agent(key)

    def _hitl_enabled(self) -> bool:
        """审批开关与请求路径读**同一个**来源。

        与 ``LangGraphOrchestrator._hitl_enabled`` 是同一段逻辑的两个副本这件事
        本身不好，但抽成共享函数要动 ADR-008 的适配器（本轮不动它）。
        真正要紧的是"读哪个变量"必须一致，否则两个入口对"要不要人工审批"
        会给出相反答案。
        """
        if self._settings is not None:
            return bool(getattr(self._settings, "hitl_enabled", False))
        from app.config import settings as default_settings

        return bool(default_settings.hitl_enabled)

    @staticmethod
    def _normalize_verdict(raw: Any, plan: tuple[SubtaskAssignment, ...]) -> CritiqueVerdict:
        """把 Critic（可能是 LLM）的自由输出收敛成合法裁决。

        两条硬规则：
        * 未知 decision 一律当 revise 且 targets 为空——"说要改却没说改谁"，
          无从重跑，于是进"未通过"分支转人工，而不是当 approve 放行。
        * targets 里越界的 index 一律丢掉：按不存在的子任务去重跑会直接崩。
        """
        payload = raw if isinstance(raw, dict) else {}
        valid = {item.index for item in plan}
        decision = str(payload.get("decision", "") or "").strip().lower()
        reasons = tuple(
            str(reason).strip()
            for reason in (payload.get("reasons") or ())
            if str(reason).strip()
        )
        if decision not in (DECISION_APPROVE, DECISION_REVISE):
            return CritiqueVerdict(
                decision=DECISION_REVISE,
                reasons=reasons or (f"critic 返回未知裁决 {decision!r}",),
                targets=(),
            )
        targets: list[int] = []
        for target in payload.get("targets") or ():
            if isinstance(target, bool) or not isinstance(target, (int, float)):
                continue
            if int(target) in valid and int(target) not in targets:
                targets.append(int(target))
        return CritiqueVerdict(decision=decision, reasons=reasons, targets=targets)

    # ── nodes ──────────────────────────────────────────────────────────────

    async def _node_plan(self, state: CollabState) -> dict[str, Any]:
        """Supervisor 拆任务。产出带 agent 归属与依赖边的子任务列表。"""
        task = str(state.get("task", "") or "").strip()
        try:
            raw = await self._llm.plan(task=task)
        except Exception as exc:  # noqa: BLE001 - 规划失败不该让整条链路崩
            logger.warning("collab plan failed, degrading to single subtask: %s", exc)
            raw = []

        # 两遍处理：第一遍收合法项并按新序号重编号（LLM 给的 index 不可信），
        # 第二遍按新序号过滤依赖边。宁可少一条边，也不留一条指向错误子任务的边。
        staged: list[tuple[int, str, str, tuple[int, ...]]] = []
        for item in list(raw or [])[: self._max_subtasks]:
            if not isinstance(item, dict):
                continue
            agent = str(item.get("agent", "") or "").strip().lower()
            if agent not in EXPERT_KEYS:
                logger.warning(
                    "collab plan: agent %r 不在专家池 %s，回落 general", agent, EXPERT_KEYS
                )
                agent = "general"
            instruction = str(item.get("instruction", "") or "").strip() or task
            depends = tuple(
                int(dep)
                for dep in (item.get("depends_on") or ())
                if isinstance(dep, (int, float)) and not isinstance(dep, bool)
            )
            staged.append((len(staged), instruction, agent, depends))

        remap = {old: new for new, (old, _, _, _) in enumerate(staged)}
        assignments = tuple(
            SubtaskAssignment(
                index=new,
                instruction=instruction,
                agent=agent,
                depends_on=tuple(
                    sorted(
                        {
                            remap[dep]
                            for dep in depends
                            if dep in remap and remap[dep] != new
                        }
                    )
                ),
            )
            for new, (_, instruction, agent, depends) in enumerate(staged)
        )
        if not assignments:
            # 规划层什么都没给出（LLM 抽风或任务为空）也要有条路可走：
            # 单个 general 子任务承载原始任务，治理链照常接管。
            logger.info("collab plan empty, falling back to a single general subtask")
            assignments = (
                SubtaskAssignment(
                    index=0,
                    instruction=task or "(空任务)",
                    agent=_pick_agent(task),
                ),
            )
        return {
            "plan": tuple(item.to_dict() for item in assignments),
            "round": 0,
            "node_path": ["plan"],
        }

    async def _node_dispatch(self, state: CollabState) -> dict[str, Any]:
        """决定本轮派哪些子任务。

        第 0 轮派全部；反思轮只派 Critic 点名的那些——"打回只重跑被打回的子任务"
        就是这一行。targets 越界（Critic 编了个不存在的 index）时退回全量，
        让图继续走而不是空转。
        """
        plan = _assignments(state.get("plan"))
        round_no = int(state.get("round", 0) or 0)
        raw_verdict = state.get("verdict")
        verdict = _to_verdict(raw_verdict) if isinstance(raw_verdict, dict) else None
        pending = plan
        if round_no > 0 and verdict is not None and verdict.targets:
            wanted = {int(target) for target in verdict.targets}
            targeted = tuple(item for item in plan if item.index in wanted)
            if targeted:
                pending = targeted
            else:
                logger.warning(
                    "collab critic targets %s 均不在计划内，本轮改派全部子任务",
                    sorted(wanted),
                )
        return {
            "pending": tuple(item.to_dict() for item in pending),
            "node_path": ["dispatch"],
        }

    def _fan_out(self, state: CollabState) -> Any:
        """dispatch 的条件边：按子任务派 ``Send``，这就是 handoff。

        返回 ``list[Send]`` 时每个 Send 各带一份子任务负载，LangGraph 在同一
        superstep 里并发执行多个 ``execute_expert``；它们的写入靠
        ``_merge_outputs`` / ``_add`` 这类 reducer 合并，谁后返回都不会丢。

        负载里必须带上 trace/session/round：``Send`` 的第二个参数是该节点的
        **完整输入**，不是"往状态里补两个字段"。少了它们，worker 里连审计要写的
        trace_id 都取不到（这是第一版实测撞出来的 KeyError）。
        """
        pending = tuple(state.get("pending") or ())
        if not pending:
            return "critic"
        shared = {
            "trace_id": state.get("trace_id", ""),
            "session_id": state.get("session_id", ""),
            "task": state.get("task", ""),
            "round": int(state.get("round", 0) or 0),
        }
        return [
            Send("execute_expert", {**shared, "subtask": dict(item)})
            for item in pending
        ]

    async def _node_execute_expert(self, state: CollabState) -> dict[str, Any]:
        """执行一个子任务：复用 ``execute_with_retry`` 与真实专家。"""
        subtask = (
            _to_assignment(state["subtask"])
            if isinstance(state.get("subtask"), dict)
            else None
        )
        if subtask is None:
            return {"node_path": ["execute_expert:?"]}
        label = f"execute_expert:{subtask.index}"
        slot: dict[str, Any] = {
            "intent": COLLAB_INTENT,
            "resource": COLLAB_INTENT,
            # 刻意不写 system_prompt：专家的 prompt 是它自己的事，协作层不覆盖，
            # 否则"协作"会顺带改掉单 Agent 路径的提示词行为。
            "system_prompt": "",
            "collab_agent": subtask.agent,
            "collab_subtask_index": subtask.index,
            "collab_round": int(state.get("round", 0) or 0),
            "collab_depends_on": list(subtask.depends_on),
        }
        envelope = AgentEnvelope(
            trace_id=state["trace_id"],
            session_id=state["session_id"],
            user_raw_input=subtask.instruction,
            global_summary="",
            agent_local_slot=slot,
        )
        updates: dict[str, Any] = {"node_path": [label], "audited_subtasks": 1}
        output = ""
        attempts = 0
        metrics: dict[str, Any] = {}

        expert = self._expert(subtask.agent)
        if expert is None:
            # 专家没注册是部署问题，重试也不会好。把失败写进 outputs 让 Critic 看到：
            # 打回 → 轮次耗尽 → 转人工，比让图悄悄少一个子任务诚实。
            logger.error("collab expert %r 未注册", subtask.agent)
            output = f"错误：专家 {subtask.agent} 未注册（协作专家池配置问题）"
            updates["failure_reasons"] = [f"子任务 {subtask.index}：专家 {subtask.agent} 未注册"]
        else:
            try:
                output, attempts, _ = await execute_with_retry(expert, envelope)
                metrics = dict(envelope.agent_local_slot.get("llm_metrics") or {})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单个子任务失败不该炸掉整轮
                attempts = int(getattr(exc, "attempts", 1) or 1)
                reason = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "collab subtask %d failed after %d attempts (%s)",
                    subtask.index, attempts, reason,
                )
                # 重试预算耗尽 → 升级人工，与请求路径的同一条闭环
                # （见 LangGraphOrchestrator._node_execute 的 escalate 分支）。
                output = FAILURE_ESCALATION_TEXT
                # raw_output 也要落：升级路径会跳过 summarize，不写的话
                # await_human 的 interrupt 负载与随后的 deliver 都是空的。
                updates["raw_output"] = FAILURE_ESCALATION_TEXT
                metrics = dict(envelope.agent_local_slot.get("llm_metrics") or {})
                if self._hitl_enabled():
                    updates["status"] = "pending_review"
                    updates["hitl_kind"] = "failure_escalation"
                else:
                    # 与请求路径同一条门：审批关闭时没有人可升级，退回错误语义。
                    # 否则"自动处理失败"会以 status=ok 交付，看着像成功。
                    updates["status"] = "error"
                    updates["error_code"] = ErrorCode.AGENT_FAILED.value
                updates["failure_reasons"] = [f"子任务 {subtask.index}：{reason}"]
                updates["retry_reason"] = reason

        cost_usd = float(metrics.get("cost_usd", 0.0) or 0.0)
        tool_calls = int(slot.get("tool_calls_total", 0) or 0)
        tool_errors = int(slot.get("tool_errors_total", 0) or 0)
        rejections = int(slot.get("tool_arg_rejections_total", 0) or 0)
        # 与 TaskAgent 同一套记账口径：成本进同一个 per-session budget guard。
        if cost_usd > 0 and self._budget_guard is not None:
            self._budget_guard.record(state["session_id"], cost_usd)

        updates.update(
            {
                "outputs": {subtask.index: output},
                "cost_usd": cost_usd,
                "llm_latency_ms": float(metrics.get("llm_latency_ms", 0.0) or 0.0),
                "tool_calls": tool_calls,
                "tool_errors": tool_errors,
                "tool_arg_rejections": rejections,
            }
        )
        await self._audit_subtask(state, subtask, output, metrics, attempts)
        return updates

    async def _node_critic(self, state: CollabState) -> dict[str, Any]:
        """Critic 评审本轮输出，给出结构化裁决。"""
        if state.get("status") in _ESCALATED_STATUSES:
            # 已有子任务重试耗尽并升级人工：不再评审，直接放行到 HITL。
            return {"node_path": ["critic"]}
        plan = [item for item in (state.get("plan") or ()) if isinstance(item, dict)]
        outputs = dict(state.get("outputs") or {})
        try:
            raw = await self._llm.critique(
                task=str(state.get("task", "") or ""), plan=plan, outputs=outputs
            )
        except Exception as exc:  # noqa: BLE001 - 评审失败降级到规则，不让链路悬住
            logger.warning("collab critique failed, falling back to rules: %s", exc)
            raw = await MockCollaborationLLM().critique(
                task=str(state.get("task", "") or ""), plan=plan, outputs=outputs
            )

        verdict = self._normalize_verdict(raw, _assignments(state.get("plan")))
        # round 是本轮编号（从 0 起），推进后表示"已完成第几轮评审"。
        round_no = int(state.get("round", 0) or 0) + 1
        looping = (
            verdict.decision == DECISION_REVISE
            and bool(verdict.targets)
            and round_no < self._max_rounds
        )
        updates: dict[str, Any] = {
            # 存 dict 而不是 dataclass：checkpoint 只应含 JSON 原生类型。
            "verdict": verdict.to_dict(),
            "critique_history": [verdict.to_dict()],
            "round": round_no,
            "node_path": [f"critic:{round_no}"],
        }
        if verdict.decision == DECISION_REVISE and not looping:
            # 到上限仍不通过，或"要改但没说改谁"：标记未决，交付时显式说明。
            updates["critique_unresolved"] = True
        return updates

    def _after_critic(self, state: CollabState) -> str:
        if state.get("status") in _ESCALATED_STATUSES:
            return "escalate"
        raw_verdict = state.get("verdict")
        verdict = _to_verdict(raw_verdict) if isinstance(raw_verdict, dict) else None
        if (
            verdict is not None
            and verdict.decision == DECISION_REVISE
            and verdict.targets
            and int(state.get("round", 0) or 0) < self._max_rounds
        ):
            return "revise"
        return "approve"

    async def _node_summarize(self, state: CollabState) -> dict[str, Any]:
        """汇总各专家输出。这是交付给 guard 的"整份协作结果"。"""
        plan = _assignments(state.get("plan"))
        outputs = dict(state.get("outputs") or {})
        parts: list[str] = []
        for item in plan:
            text = str(outputs.get(item.index, "") or "").strip()
            if not text:
                continue
            parts.append(f"[{item.agent} · 子任务 {item.index}] {text}")
        body = "\n\n".join(parts) if parts else "（协作未产生任何输出）"
        rounds = int(state.get("round", 0) or 0)
        text = f"多 Agent 协作结果（{len(plan)} 个子任务，评审 {rounds} 轮）\n\n{body}"
        if state.get("critique_unresolved"):
            # 不把没通过的输出伪装成成功：前置一句显式标注。
            text = (
                "【critic 未通过】已达反思轮次上限，以下结果未经评审确认，"
                "请人工复核后再使用。\n\n" + text
            )
        return {"raw_output": text, "node_path": ["summarize"]}

    async def _node_evaluate(self, state: CollabState) -> dict[str, Any]:
        result = await self._evaluator.score(
            state.get("raw_output", ""), COLLAB_INTENT
        )
        return {
            "eval_score": float(result.score),
            "eval_issues": tuple(getattr(result, "issues", ()) or ()),
            "node_path": ["evaluate"],
        }

    async def _node_guard(self, state: CollabState) -> dict[str, Any]:
        """三级 guard 合并——与请求路径**同一份**判定逻辑。

        注意这里没有 ``_task_degradation_issues``：协作链路不走 task-LLM
        的 ReAct 降级路径（专家池里没有 ``task``），没有"降级原因"可转换。
        其余三个来源一个不少。
        """
        raw_output = state.get("raw_output", "")
        payload = {
            "intent": COLLAB_INTENT,
            "resource": COLLAB_INTENT,
            "role": settings.default_role,
        }
        guard_intent = COLLAB_INTENT
        hitl_enabled = self._hitl_enabled()
        if "EXECUTION_REQUIRES_APPROVAL" in raw_output:
            guard_intent = "execute_code"
            hitl_enabled = True

        verdict = self._guard.evaluate(
            COLLAB_AGENT_NAME, guard_intent, payload, hitl_enabled=hitl_enabled
        )
        policy_ids: tuple[str, ...] = ()
        hitl_kind = ""
        if verdict.action != GuardianAction.DENY:
            try:
                role = Role(payload.get("role", "operator"))
            except ValueError:
                role = None
            try:
                output_verdict, policy_ids = self._guard.evaluate_output(
                    raw_output, intent=guard_intent, role=role, hitl_enabled=hitl_enabled
                )
            except Exception:  # noqa: BLE001 - mirrored from the request path
                output_verdict = GuardVerdict(action=GuardianAction.ALLOW, reason="ok")
                policy_ids = ()
            eval_verdict = _verdict_from_eval_issues(
                tuple(state.get("eval_issues", ()) or ())
                + _tool_failure_issues(
                    int(state.get("tool_calls", 0) or 0),
                    int(state.get("tool_errors", 0) or 0),
                    int(state.get("tool_arg_rejections", 0) or 0),
                )
            )
            merged = _merge_guard(verdict, output_verdict, eval_verdict)
            if eval_verdict is not None and merged is eval_verdict:
                hitl_kind = (
                    "eval_deny"
                    if eval_verdict.action == GuardianAction.DENY
                    else "eval_review"
                )
            verdict = merged

        need_review = bool(state.get("critique_unresolved"))
        if (
            need_review
            and verdict.action == GuardianAction.ALLOW
            and self._hitl_enabled()
        ):
            # Critic 没通过时，即使 guard 放行也要转人工——"未经确认的交付"
            # 不该以 status=ok 结束。
            verdict = GuardVerdict(
                action=GuardianAction.REVIEW, reason="critic 未通过，需人工复核"
            )
            hitl_kind = hitl_kind or "critic_unresolved"

        return {
            "guard_action": verdict.action.value,
            "guard_reason": verdict.reason,
            "policy_hits": policy_ids,
            "hitl_kind": hitl_kind,
            "need_human_review": need_review,
            "node_path": ["guard"],
        }

    def _after_guard(self, state: CollabState) -> str:
        action = state.get("guard_action", GuardianAction.ALLOW.value)
        if action == GuardianAction.DENY.value:
            return "deny"
        if action == GuardianAction.REVIEW.value:
            return "review"
        return "allow"

    async def _node_prepare_hitl(self, state: CollabState) -> dict[str, Any]:
        """挂起前的准备节点。

        与 ``await_human`` 分成两个节点是 ``interrupt()`` 的重放规则要求的：
        被中断的节点在 resume 时会从第一行重新执行。若把"记下挂起原因"和
        "等人"写在同一个节点里，每次 resume 都会重写一遍，``pending_review``
        也永远落不进 checkpoint。
        """
        hitl_kind = state.get("hitl_kind")
        if not hitl_kind:
            hitl_kind = (
                "failure_escalation"
                if state.get("retry_reason")
                else "review"
            )
        return {
            "hitl_kind": hitl_kind,
            # status=error（审批关闭时的失败升级）不能被覆盖成 pending_review：
            # 那会把"没有人可升级"伪装成"正在等审批"。
            "status": state.get("status") or "pending_review",
            "node_path": ["prepare_hitl"],
        }

    async def _node_await_human(self, state: CollabState) -> dict[str, Any]:
        """停在这里等人工决定。resume 时本节点从第一行重放，保持纯净。"""
        if state.get("status") == "error":
            # 失败升级且审批关闭：prepare_hitl 已经把它带到这儿，
            # 这里不能再当作"等人"，直接以错误收口。
            return {"hitl_decision": "error", "node_path": ["await_human"]}
        if not self._hitl_enabled():
            # 审批关闭时没有人会来 resume：与其让协作链路永远挂在一个 interrupt 上，
            # 不如交付并带着"未经人工确认"的标记。这与 ADR-022 的约定一致——
            # 可以交付没通过评审的结果，但不许把它说成成功的。
            logger.warning(
                "collab hitl disabled; delivering unapproved output trace=%s",
                state.get("trace_id", ""),
            )
            return {"hitl_decision": "skipped", "node_path": ["await_human"]}
        decision = interrupt(
            {
                "kind": "collab_hitl",
                "hitl_kind": state.get("hitl_kind", "review"),
                "trace_id": state.get("trace_id", ""),
                "session_id": state.get("session_id", ""),
                "task": state.get("task", ""),
                "guard_reason": state.get("guard_reason", ""),
                "policy_hits": list(state.get("policy_hits", ())),
                "agent_output": str(state.get("raw_output", ""))[:_AUDIT_EXCERPT],
                "node_path": list(state.get("node_path", ())),
            }
        )
        normalized = (
            "approve"
            if str(decision).lower() in ("approve", "approved", "ok", "true")
            else "reject"
        )
        return {"hitl_decision": normalized, "node_path": ["await_human"]}

    def _after_human(self, state: CollabState) -> str:
        # "skipped" = HITL 关闭时的自动放行，按 approve 走同一条交付腿；
        # "error" = 失败升级且审批关闭，走错误收口腿。
        if state.get("hitl_decision") == "error":
            return "failed"
        return (
            "approve"
            if state.get("hitl_decision") in ("approve", "skipped")
            else "reject"
        )

    async def _node_deliver(self, state: CollabState) -> dict[str, Any]:
        raw_output = state.get("raw_output", "")
        response = self._adapter.adapt(
            raw_output, channel=state.get("channel", ""), target=state.get("target", "")
        )
        self._memory.add(state["session_id"], state["task"], response.text)
        need_review = bool(state.get("critique_unresolved") or state.get("need_human_review"))
        await self._audit_final(state, status="ok", text=response.text, need_review=need_review)
        return {
            "delivered_text": response.text,
            "status": "approved" if state.get("hitl_decision") == "approve" else "ok",
            "need_human_review": need_review,
            "node_path": ["deliver"],
        }

    async def _node_blocked(self, state: CollabState) -> dict[str, Any]:
        text = state.get("guard_reason", "blocked by guard")
        await self._audit_final(state, status="blocked", text=text, need_review=True)
        return {
            "delivered_text": text,
            "status": "blocked",
            "need_human_review": True,
            "node_path": ["blocked"],
        }

    async def _node_rejected(self, state: CollabState) -> dict[str, Any]:
        text = "人工驳回了本次协作结果"
        await self._audit_final(state, status="rejected", text=text, need_review=True)
        return {
            "delivered_text": text,
            "status": "rejected",
            "need_human_review": True,
            "node_path": ["rejected"],
        }

    async def _node_failed(self, state: CollabState) -> dict[str, Any]:
        """子任务重试耗尽、且审批关闭：以错误收口，不伪装成交付。"""
        text = FAILURE_ESCALATION_TEXT
        await self._audit_final(state, status="error", text=text, need_review=True)
        return {
            "delivered_text": text,
            "status": "error",
            "error_code": state.get("error_code") or ErrorCode.AGENT_FAILED.value,
            "need_human_review": True,
            "node_path": ["failed"],
        }

    # ── audit ─────────────────────────────────────────────────────────────

    async def _audit_subtask(
        self,
        state: CollabState,
        subtask: SubtaskAssignment,
        output: str,
        metrics: dict[str, Any],
        attempts: int,
    ) -> None:
        """每个子任务落一条，与同一次协作的其它条目共享 trace_id。"""
        entry = AuditEntry(
            trace_id=state["trace_id"],
            session_id=state["session_id"],
            agent_name=subtask.agent,
            agent_output=(output or "")[:_AUDIT_EXCERPT],
            intent=COLLAB_INTENT,
            extra={
                "collab_stage": "subtask",
                "collab_agent": subtask.agent,
                "collab_subtask_index": subtask.index,
                "collab_round": int(state.get("round", 0) or 0),
                "collab_attempts": int(attempts or 0),
                "llm_model": str(metrics.get("model_used", "") or ""),
                "cost_usd": float(metrics.get("cost_usd", 0.0) or 0.0),
                "llm_latency_ms": float(metrics.get("llm_latency_ms", 0.0) or 0.0),
            },
        )
        try:
            await record(entry)
        except Exception:  # noqa: BLE001 - 审计失败不该拖垮协作
            logger.warning("collab subtask audit write failed", exc_info=True)

    async def _audit_final(
        self, state: CollabState, *, status: str, text: str, need_review: bool
    ) -> None:
        """一次协作的聚合条目：结论、轮次、成本、执行轨迹。"""
        history = [
            _to_verdict(item).to_dict()
            for item in (state.get("critique_history") or ())
            if isinstance(item, dict)
        ]
        plan = _assignments(state.get("plan"))
        entry = AuditEntry(
            trace_id=state["trace_id"],
            session_id=state["session_id"],
            agent_name=COLLAB_AGENT_NAME,
            agent_output=(text or "")[:_AUDIT_EXCERPT],
            intent=COLLAB_INTENT,
            eval_score=state.get("eval_score"),
            eval_issues=tuple(state.get("eval_issues") or ()),
            guard_action=state.get("guard_action", ""),
            guard_reason=state.get("guard_reason", ""),
            policy_hits=tuple(state.get("policy_hits") or ()),
            extra={
                "collab_stage": "final",
                "collab_status": status,
                "collab_rounds": int(state.get("round", 0) or 0),
                "collab_subtasks": len(plan),
                "collab_agents": sorted({item.agent for item in plan}),
                "collab_need_human_review": bool(need_review),
                "collab_critique_unresolved": bool(state.get("critique_unresolved")),
                "collab_critique_history": history,
                "collab_node_path": list(state.get("node_path") or ()),
                "cost_usd": float(state.get("cost_usd", 0.0) or 0.0),
                "llm_latency_ms": float(state.get("llm_latency_ms", 0.0) or 0.0),
                "tool_calls": int(state.get("tool_calls", 0) or 0),
                "tool_errors": int(state.get("tool_errors", 0) or 0),
                "tool_arg_rejections": int(state.get("tool_arg_rejections", 0) or 0),
            },
        )
        try:
            await record(entry)
        except Exception:  # noqa: BLE001 - 审计失败不该拖垮协作
            logger.warning("collab final audit write failed", exc_info=True)

    # ── public API ────────────────────────────────────────────────────────

    def _config(self, thread_id: str) -> dict[str, Any]:
        """Checkpoint key，与 ``LangGraphOrchestrator`` 同理按 trace_id 键。"""
        return {"configurable": {"thread_id": thread_id}}

    def _to_result(self, state: dict[str, Any]) -> CollabResult:
        interrupted = "__interrupt__" in state
        status = "pending_review" if interrupted else state.get("status", "ok")
        outputs = dict(state.get("outputs") or {})
        plan = _assignments(state.get("plan"))
        per_agent: dict[str, list[str]] = {}
        for item in plan:
            text = str(outputs.get(item.index, "") or "").strip()
            if text:
                per_agent.setdefault(item.agent, []).append(text)
        need_review = bool(state.get("need_human_review") or state.get("critique_unresolved"))
        if status in ("pending_review", "rejected", "blocked"):
            need_review = True
        return CollabResult(
            trace_id=state.get("trace_id", ""),
            task=state.get("task", ""),
            plan=plan,
            rounds=int(state.get("round", 0) or 0),
            per_agent_outputs={key: "\n\n".join(value) for key, value in per_agent.items()},
            critique_history=_verdict(state.get("critique_history")),
            final_text=state.get("delivered_text", "") or "",
            status=status,
            need_human_review=need_review,
            cost_usd=float(state.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=float(state.get("llm_latency_ms", 0.0) or 0.0),
            tool_calls=int(state.get("tool_calls", 0) or 0),
            node_path=tuple(state.get("node_path") or ()),
            guard_action=state.get("guard_action", ""),
            error_code=state.get("error_code", ""),
        )

    async def run(
        self,
        *,
        task: str,
        trace_id: str | None = None,
        session_id: str = "",
        channel: str = "",
        target: str = "",
        user_id: str = "",
    ) -> CollabResult:
        """跑一次协作。返回 ``CollabResult``（含中断时的挂起状态）。"""
        trace_id = trace_id or new_trace_id()
        sid = session_id or f"collab:{trace_id}"
        bind_trace(trace_id)
        initial: CollabState = {
            "trace_id": trace_id,
            "session_id": sid,
            "user_id": user_id,
            "task": str(task or "").strip(),
            "channel": channel,
            "target": target,
            "node_path": [],
        }
        state = await self._graph.ainvoke(initial, self._config(trace_id))
        return self._to_result(state)

    async def resume(self, thread_id: str, decision: str) -> CollabResult:
        """放行一次挂起——HITL 的 ``Command(resume=...)`` 那一半。"""
        state = await self._graph.ainvoke(
            Command(resume=decision), self._config(thread_id)
        )
        return self._to_result(state)

    def pending_payload(self, thread_id: str) -> dict[str, Any] | None:
        """查看某个挂起的 interrupt 负载（无副作用）。"""
        snapshot = self._graph.get_state(self._config(thread_id))
        interrupts = getattr(snapshot, "interrupts", ()) or ()
        if not interrupts:
            return None
        value = interrupts[0].value
        return dict(value) if isinstance(value, dict) else {"value": value}

    def describe(self) -> dict[str, str]:
        return {"pipeline": "collaboration"}


__all__ = [
    "COLLAB_AGENT_NAME",
    "COLLAB_INTENT",
    "EXPERT_KEYS",
    "CollabOrchestrator",
    "CollabResult",
    "CollabState",
    "CollaborationLLM",
    "CritiqueVerdict",
    "LiteLLMCollaborationLLM",
    "MockCollaborationLLM",
    "ScriptedExpert",
    "SubtaskAssignment",
    "build_collaboration_llm",
]
