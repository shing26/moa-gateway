"""多 Agent 协作链路的测试（ADR-022 / R6）。

这些用例守的是三件事，按重要性排：

1. **治理不旁路**。协作路径上 deny / review / allow 三态的判定与单 Agent 路径
   一致——因为用的是同一份 ``_merge_guard``。这条松了，"多 Agent 在治理之下"
   就只是 docstring 里的一句话。
2. **有界与诚实**。反思回边必须停在 ``COLLAB_MAX_ROUNDS``；到上限仍不通过必须
   标 ``need_human_review`` 并在文本里说出来，不能把没通过的输出当成功交付。
3. **handoff 归属正确**。Supervisor 说给 coder 的子任务，就得由 coder 执行、
   并按 coder 记审计。

全部离线：专家用 ``ScriptedExpert``/记录型替身，LLM 用规则式 mock，
零网络零 token。langgraph 是可选 extra，所以本文件整体 skipif 不可用。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.orchestration.collaboration import (
    COLLAB_INTENT,
    EXPERT_KEYS,
    CollabOrchestrator,
    CollabResult,
    LiteLLMCollaborationLLM,
    MockCollaborationLLM,
    ScriptedExpert,
    SubtaskAssignment,
    expert_failure_reason,
)

pytest.importorskip("langgraph", reason="langgraph 是可选 extra；未安装时跳过协作图测试")


class RecordingExpert:
    """记录"哪个专家收到了哪条指令"的替身专家。"""

    def __init__(self, name: str, *, fail_times: int = 0) -> None:
        self.name = name
        self.seen: list[str] = []
        self.fail_times = fail_times
        self._calls = 0

    async def execute(self, envelope):
        self._calls += 1
        if self._calls <= self.fail_times:
            raise RuntimeError(f"{self.name} 基础设施故障 #{self._calls}")
        self.seen.append(envelope.user_raw_input)
        slot = dict(envelope.agent_local_slot or {})
        return f"{self.name} 输出的结论：{envelope.user_raw_input.strip()[:40]}"


class StubLLM:
    """可编程的规划/评审替身。"""

    def __init__(self, plan, critiques=None) -> None:
        self._plan = plan
        self._critiques = list(critiques or [])
        self.critique_calls = 0

    async def plan(self, *, task: str):
        return self._plan(task)

    async def critique(self, *, task: str, plan, outputs):
        self.critique_calls += 1
        if self._critiques:
            verdict = self._critiques.pop(0)
        else:
            verdict = {"decision": "approve", "reasons": [], "targets": []}
        return verdict(self) if callable(verdict) else verdict


def _cfg(**overrides):
    base = {
        "collab_llm": "mock",
        "collab_max_rounds": 2,
        "collab_max_subtasks": 4,
        "hitl_enabled": False,
        "default_role": "operator",
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _experts(**overrides):
    pool = {
        "coder": RecordingExpert("coder"),
        "general": RecordingExpert("general"),
        "review": RecordingExpert("review"),
    }
    pool.update(overrides)
    return pool


TWO_SUBTASKS = [
    {"index": 0, "instruction": "写一个函数", "agent": "coder", "depends_on": []},
    {"index": 1, "instruction": "审查上面的函数", "agent": "review", "depends_on": [0]},
]


# ── 规划与 handoff ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_plan_splits_and_stays_inside_expert_pool() -> None:
    llm = MockCollaborationLLM(max_subtasks=3)
    plan = await llm.plan(task="写一个 Python 函数，然后审查它，并且总结要点")
    assert len(plan) == 3
    assert [item["index"] for item in plan] == [0, 1, 2]
    # 分专家只能在专家池里，否则 execute_expert 会拿到 get_agent(key)=None
    assert all(item["agent"] in EXPERT_KEYS for item in plan)
    assert plan[0]["agent"] == "coder"
    assert plan[1]["agent"] == "review"


@pytest.mark.asyncio
async def test_mock_plan_respects_max_subtasks() -> None:
    llm = MockCollaborationLLM(max_subtasks=2)
    plan = await llm.plan(task="甲，然后乙，然后丙，然后丁，然后戊")
    assert len(plan) == 2


@pytest.mark.asyncio
async def test_handoff_routes_each_subtask_to_its_assigned_expert() -> None:
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts=experts,
        settings_obj=_cfg(),
    )
    result = await orch.run(task="写函数再审查", trace_id="t-handoff")

    assert experts["coder"].seen == ["写一个函数"]
    assert experts["review"].seen == ["审查上面的函数"]
    # general 不该被碰：分工是 Supervisor 说了算，不是"谁都跑一遍"
    assert experts["general"].seen == []
    assert result.status == "ok"
    assert len(result.per_agent_outputs) == 2


@pytest.mark.asyncio
async def test_plan_agent_outside_pool_falls_back_to_general() -> None:
    """LLM 编了个不存在的专家：降级到 general，而不是让图空转。"""
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "干点啥", "agent": "wizard", "depends_on": []}
            ]
        ),
        experts=experts,
        settings_obj=_cfg(),
    )
    result = await orch.run(task="随便", trace_id="t-pool")
    assert [item.agent for item in result.plan] == ["general"]
    assert experts["general"].seen == ["干点啥"]


@pytest.mark.asyncio
async def test_empty_plan_degrades_to_one_general_subtask() -> None:
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: []),
        experts=experts,
        settings_obj=_cfg(),
    )
    result = await orch.run(task="做点什么", trace_id="t-empty")
    assert len(result.plan) == 1
    assert result.plan[0].agent in EXPERT_KEYS
    assert result.plan[0].instruction == "做点什么"
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_subtask_cap_truncates_plan() -> None:
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts=experts,
        settings_obj=_cfg(collab_max_subtasks=1),
    )
    result = await orch.run(task="x", trace_id="t-cap")
    assert len(result.plan) == 1
    assert experts["review"].seen == []


# ── Critic 反思回边 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revise_reruns_only_the_targeted_subtask() -> None:
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: TWO_SUBTASKS,
            critiques=[
                {"decision": "revise", "reasons": ["第二个不够具体"], "targets": [1]},
                {"decision": "approve", "reasons": [], "targets": []},
            ],
        ),
        experts=experts,
        settings_obj=_cfg(),
    )
    result = await orch.run(task="写函数再审查", trace_id="t-revise")

    # 打回 1，那么 1 跑了两轮、0 只跑了一轮
    assert experts["review"].seen == ["审查上面的函数", "审查上面的函数"]
    assert experts["coder"].seen == ["写一个函数"]
    assert result.rounds == 2
    assert result.status == "ok"
    assert [v.decision for v in result.critique_history] == ["revise", "approve"]
    # 执行轨迹里第二轮只有被打回的那个
    assert result.node_path.count("execute_expert:1") == 2
    assert result.node_path.count("execute_expert:0") == 1


@pytest.mark.asyncio
async def test_max_rounds_bounds_the_reflection_back_edge() -> None:
    always_revise = lambda llm: {  # noqa: E731
        "decision": "revise", "reasons": ["永远不满意"], "targets": [0]
    }
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS, critiques=[always_revise] * 10),
        experts=experts,
        settings_obj=_cfg(collab_max_rounds=2),
    )
    result = await orch.run(task="x", trace_id="t-bounded")

    assert result.rounds == 2
    assert result.critique_history[-1].decision == "revise"
    assert result.need_human_review is True
    # 到上限仍不通过：**必须说出来**，不能伪装成成功
    assert "critic 未通过" in result.final_text
    # 依赖链按 wave 返回 dispatch；两轮各含两个执行层和一个收敛到 Critic 的
    # 空派发，因此是 6 次，而不是无限空转。
    assert result.node_path.count("dispatch") == 6
    assert result.node_path.count("critic:3") == 0


@pytest.mark.asyncio
async def test_revise_without_targets_is_treated_as_unresolved() -> None:
    """"要改却没说要改谁"无从重跑，只能转人工——不能当 approve 放行。"""
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: TWO_SUBTASKS,
            critiques=[{"decision": "revise", "reasons": ["整体不对"], "targets": []}],
        ),
        experts=experts,
        settings_obj=_cfg(hitl_enabled=True),
    )
    result = await orch.run(task="x", trace_id="t-notargets")
    assert result.status == "pending_review"
    assert experts["coder"].seen == ["写一个函数"]  # 没有重跑


@pytest.mark.asyncio
async def test_unknown_critic_decision_is_not_read_as_approval() -> None:
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: TWO_SUBTASKS,
            critiques=[{"decision": "maybe", "reasons": [], "targets": []}],
        ),
        experts=experts,
        settings_obj=_cfg(hitl_enabled=True),
    )
    result = await orch.run(task="x", trace_id="t-unknown")
    assert result.status == "pending_review"
    assert result.critique_history[-1].decision == "revise"


# ── 治理：deny / review / allow ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_deny_blocks_the_collaboration() -> None:
    experts = _experts(review=RecordingExpert("review"))
    leak = ScriptedExpert(reply="AKIA1234567890ABCDEF 是访问密钥")
    experts["review"] = leak
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts=experts,
        settings_obj=_cfg(),
    )
    result = await orch.run(task="x", trace_id="t-deny")

    assert result.status == "blocked"
    assert result.guard_action == "deny"
    assert "deny" in result.final_text.lower() or "policy" in result.final_text.lower()
    # 被拦的协作不该留下一条"成功交付"的审计结论
    assert result.need_human_review is True


@pytest.mark.asyncio
async def test_guard_review_suspends_and_resume_approves() -> None:
    experts = _experts(general=ScriptedExpert(reply="TODO: 还没写完"))
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "写点啥", "agent": "general", "depends_on": []}
            ]
        ),
        experts=experts,
        settings_obj=_cfg(hitl_enabled=True),
    )
    suspended = await orch.run(task="x", trace_id="t-review")

    assert suspended.status == "pending_review"
    assert suspended.guard_action == "review"
    payload = orch.pending_payload("t-review")
    assert payload is not None
    assert payload["kind"] == "collab_hitl"
    assert payload["trace_id"] == "t-review"

    approved = await orch.resume("t-review", "approve")
    assert approved.status == "approved"
    assert "TODO" in approved.final_text


@pytest.mark.asyncio
async def test_guard_review_suspends_and_resume_rejects() -> None:
    experts = _experts(general=ScriptedExpert(reply="TODO: 还没写完"))
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "写点啥", "agent": "general", "depends_on": []}
            ]
        ),
        experts=experts,
        settings_obj=_cfg(hitl_enabled=True),
    )
    await orch.run(task="x", trace_id="t-reject")
    rejected = await orch.resume("t-reject", "reject")
    assert rejected.status == "rejected"
    assert rejected.need_human_review is True


@pytest.mark.asyncio
async def test_clean_output_takes_the_allow_edge() -> None:
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts=_experts(),
        settings_obj=_cfg(),
    )
    result = await orch.run(task="x", trace_id="t-allow")
    assert result.status == "ok"
    assert result.guard_action == "allow"
    assert result.node_path[-1] == "deliver"


# ── 失败升级 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_exhaustion_escalates_to_human() -> None:
    experts = _experts(general=RecordingExpert("general", fail_times=5))
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "做", "agent": "general", "depends_on": []}
            ]
        ),
        experts=experts,
        settings_obj=_cfg(hitl_enabled=True),
    )
    result = await orch.run(task="x", trace_id="t-escalate")

    assert result.status == "pending_review"
    payload = orch.pending_payload("t-escalate")
    assert payload["hitl_kind"] == "failure_escalation"
    # 失败升级不该再跑评审：critic 节点只是路过
    assert "critic:1" not in result.node_path


@pytest.mark.asyncio
async def test_failure_escalation_with_hitl_off_is_an_error_not_a_success() -> None:
    """审批关闭时没有人可升级：必须退回错误语义，不能 status=ok 交付。"""
    experts = _experts(general=RecordingExpert("general", fail_times=5))
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "做", "agent": "general", "depends_on": []}
            ]
        ),
        experts=experts,
        settings_obj=_cfg(hitl_enabled=False),
    )
    result = await orch.run(task="x", trace_id="t-escalate-off")
    assert result.status == "error"
    assert result.error_code == "agent_failed"
    assert result.node_path[-1] == "failed"


@pytest.mark.asyncio
async def test_missing_expert_is_a_failed_subtask_not_a_silent_gap(monkeypatch) -> None:
    """专家池里有人没注册：那条子任务必须显式失败，不能从结果里悄悄消失。

    不能靠"不注入 review"来造这个场景——``_expert()`` 会回落到
    ``AGENT_REGISTRY``，而 registry 里 review 是真注册了的。所以这里直接
    monkeypatch 模块里的 ``get_agent``。
    """
    import app.orchestration.collaboration as collab

    # 注入字典里**不能**有 review，否则 _expert() 先命中注入项，
    # 根本走不到 get_agent 那条回落路径。
    experts = {k: v for k, v in _experts().items() if k != "review"}
    monkeypatch.setattr(
        collab,
        "get_agent",
        lambda name: None if name == "review" else experts.get(name),
    )
    orch = CollabOrchestrator(
        # 规划指定 review（否则 mock 会把 "x" 判给 general），
        # 评审交给规则式 mock：它会真的去看输出里的失败标记。
        collaboration_llm=_PlanThenMockCritique(
            [{"index": 0, "instruction": "审查", "agent": "review", "depends_on": []}]
        ),
        experts=experts,
        settings_obj=_cfg(),
    )
    result = await orch.run(task="x", trace_id="t-missing")
    # 子任务标记为失败 → Critic 打回 → 轮次耗尽 → 未决
    assert result.need_human_review is True
    assert "未注册" in result.final_text
    assert "critic 未通过" in result.final_text
    assert result.rounds == 2
    assert [v.decision for v in result.critique_history] == ["revise", "revise"]


class _PlanThenMockCritique:
    """规划用固定列表，评审交给规则式 mock。"""

    def __init__(self, plan) -> None:
        self._plan = plan
        self._mock = MockCollaborationLLM()

    async def plan(self, *, task: str):
        return self._plan

    async def critique(self, **kwargs):
        return await self._mock.critique(**kwargs)


# ── 审计 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_writes_one_entry_per_subtask_plus_one_aggregate(tmp_path, monkeypatch) -> None:
    from app.audit import recorder

    entries: list = []

    async def fake_record(entry):
        entries.append(entry)

    monkeypatch.setattr(recorder, "record", fake_record)
    import app.orchestration.collaboration as collab

    monkeypatch.setattr(collab, "record", fake_record)

    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts=_experts(),
        settings_obj=_cfg(),
    )
    await orch.run(task="x", trace_id="t-audit", session_id="s-audit")

    subtask_entries = [e for e in entries if e.extra.get("collab_stage") == "subtask"]
    final_entries = [e for e in entries if e.extra.get("collab_stage") == "final"]
    assert len(subtask_entries) == 2
    assert len(final_entries) == 1
    # 共享 trace_id，否则按 trace 捞不出一条链
    assert {e.trace_id for e in entries} == {"t-audit"}
    # 子任务条目写真实专家名，聚合条目不冒名
    assert {e.agent_name for e in subtask_entries} == {"coder", "review"}
    assert final_entries[0].agent_name == "collaboration"
    # 子任务序号与轮次要能区分同一专家的多次执行
    assert sorted(e.extra["collab_subtask_index"] for e in subtask_entries) == [0, 1]
    assert final_entries[0].intent == COLLAB_INTENT


# ── 图本身 ──────────────────────────────────────────────────────────────


def test_graph_compiles_with_the_documented_nodes() -> None:
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts=_experts(),
        settings_obj=_cfg(),
    )
    graph = orch._graph
    assert graph is not None
    nodes = set(graph.get_graph().nodes)
    for expected in (
        "plan",
        "dispatch",
        "execute_expert",
        "critic",
        "summarize",
        "evaluate",
        "guard",
        "prepare_hitl",
        "await_human",
        "deliver",
        "blocked",
        "rejected",
        "failed",
    ):
        assert expected in nodes, f"图里少了 {expected} 节点"


@pytest.mark.asyncio
async def test_offline_determinism_same_input_same_node_path() -> None:
    async def one_run():
        orch = CollabOrchestrator(
            collaboration_llm=MockCollaborationLLM(max_subtasks=3),
            experts={k: ScriptedExpert() for k in EXPERT_KEYS},
            settings_obj=_cfg(),
        )
        return await orch.run(
            task="写一个 Python 函数，然后审查它，并且总结要点", trace_id="t-det"
        )

    first = await one_run()
    second = await one_run()
    assert first.node_path == second.node_path
    assert first.final_text == second.final_text
    assert first.plan == second.plan


@pytest.mark.asyncio
async def test_result_carries_cost_and_tool_counters() -> None:
    experts = _experts()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts=experts,
        settings_obj=_cfg(),
    )
    result = await orch.run(task="x", trace_id="t-cost")
    assert isinstance(result, CollabResult)
    # ScriptedExpert/RecordingExpert 不记账，所以恒 0；这里断言的是"字段存在且是数"
    assert result.cost_usd == 0.0
    assert result.tool_calls == 0
    assert all(isinstance(s, SubtaskAssignment) for s in result.plan)


@pytest.mark.asyncio
async def test_session_id_defaults_to_a_collab_namespaced_value() -> None:
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "做", "agent": "general", "depends_on": []}
            ]
        ),
        experts=_experts(),
        settings_obj=_cfg(),
    )
    result = await orch.run(task="x", trace_id="t-sid")
    # 不给 session_id 时用协作自己的前缀，避免与请求路径的会话撞
    assert result.trace_id == "t-sid"


# ── 协作层自身的成本记账 ──────────────────────────────────────────────────


class _SpendingLLM:
    """每次调用都记账的协作 LLM 替身（验证 plan/critique 的开销真的进了成本）。"""

    def __init__(self, plan, critiques=None) -> None:
        self._plan = plan
        self._critiques = list(critiques or [])
        self._spend = {"cost_usd": 0.0, "llm_latency_ms": 0.0, "calls": 0}

    async def plan(self, *, task: str):
        self._spend["cost_usd"] += 0.11
        self._spend["llm_latency_ms"] += 100.0
        self._spend["calls"] += 1
        return self._plan

    async def critique(self, **kwargs):
        self._spend["cost_usd"] += 0.22
        self._spend["llm_latency_ms"] += 200.0
        self._spend["calls"] += 1
        if self._critiques:
            return self._critiques.pop(0)
        return {"decision": "approve", "reasons": [], "targets": []}

    def take_metrics(self) -> dict[str, float]:
        out = dict(self._spend)
        self._spend = {"cost_usd": 0.0, "llm_latency_ms": 0.0, "calls": 0}
        return out


@pytest.mark.asyncio
async def test_collaboration_layer_spend_lands_in_cost() -> None:
    """plan 与 critique 的 token 开销必须计入 cost_usd。

    此前只有专家侧 llm_metrics 进成本，协作层自己花的钱恒为 0——跑两轮反思
    和一轮放行在成本上看不出区别。
    """
    orch = CollabOrchestrator(
        collaboration_llm=_SpendingLLM(TWO_SUBTASKS),
        experts=_experts(),
        settings_obj=_cfg(),
    )
    result = await orch.run(task="规划一次，评审一次", trace_id="t-spend")
    # 一次 plan + 一次 critique = 0.11 + 0.22；专家侧是 ScriptedExpert/Recording
    # 替身，不产生 llm_metrics。
    assert result.cost_usd == pytest.approx(0.33)
    assert result.llm_latency_ms == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_spend_is_counted_once_not_doubled_across_nodes() -> None:
    """同一段花费不能被多个节点重复累加（take 走的是取走即清零）。"""
    llm = _SpendingLLM(TWO_SUBTASKS)
    orch = CollabOrchestrator(
        collaboration_llm=llm,
        experts=_experts(),
        settings_obj=_cfg(),
    )
    result = await orch.run(task="规划一次，评审一次", trace_id="t-spend-once")
    # 两次调用各记一次：0.33 说明既没漏记也没重复计
    assert result.cost_usd == pytest.approx(0.33)
    assert result.rounds == 1


def test_mock_task_llm_take_metrics_is_zero() -> None:
    """mock 后端的 take_metrics 必须存在且恒为 0，否则编排层要为两种后端分叉。"""
    assert MockCollaborationLLM().take_metrics() == {
        "cost_usd": 0.0,
        "llm_latency_ms": 0.0,
        "calls": 0,
    }


def test_build_collaboration_llm_respects_subtask_cap() -> None:
    """mock 后端的上限必须来自配置，而不是写死的常数。"""
    from app.orchestration.collaboration import build_collaboration_llm

    cfg = _cfg(collab_llm="mock", collab_max_subtasks=2)
    llm = build_collaboration_llm(cfg)
    assert isinstance(llm, MockCollaborationLLM)
    assert llm._max_subtasks == 2


def test_stream_and_finish_agree_with_run(tmp_path, monkeypatch) -> None:
    """``run_streamed`` + ``finish`` 与一次性 ``run`` 必须给出同一份结果。"""
    import asyncio

    from app.audit import recorder

    async def fake_record(entry):
        return None

    monkeypatch.setattr(recorder, "record", fake_record)
    import app.orchestration.collaboration as collab

    monkeypatch.setattr(collab, "record", fake_record)

    async def _collect(streamed: bool) -> CollabResult:
        orch = CollabOrchestrator(
            collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
            experts=_experts(),
            settings_obj=_cfg(),
        )
        if not streamed:
            return await orch.run(task="一起做两件事", trace_id="t-parity")
        events = []
        async for event in orch.run_streamed(task="一起做两件事", trace_id="t-parity"):
            events.append(event)
        assert events, "流式模式一条事件都不该少"
        return orch.finish("t-parity")

    one_shot = asyncio.run(_collect(False))
    streamed = asyncio.run(_collect(True))
    assert streamed.node_path == one_shot.node_path
    assert streamed.final_text == one_shot.final_text
    assert streamed.status == one_shot.status
    assert streamed.cost_usd == one_shot.cost_usd


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("critiques", "expected_hint"),
    [
        # 一直打回且点名了子任务：撞上轮次上限
        (
            [
                {"decision": "revise", "reasons": ["r1"], "targets": [0]},
                {"decision": "revise", "reasons": ["r2"], "targets": [0]},
                {"decision": "revise", "reasons": ["r3"], "targets": [0]},
            ],
            "已达反思轮次上限",
        ),
        # 打回但没点名：根本谈不到上限
        (
            [{"decision": "revise", "reasons": ["r"], "targets": []}],
            "未指明改哪些子任务",
        ),
    ],
)
async def test_unresolved_critique_says_which_case_it_is(critiques, expected_hint) -> None:
    """未通过交付的标注必须说清是哪种未决。

    "已达反思轮次上限"和"评审要求改但没说改谁"是两回事。把前者的话术安给
    后者，等于拿一件没发生的事当解释——而这句文本是交给用户看的。
    """
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS, critiques=critiques),
        experts=_experts(),
        settings_obj=_cfg(collab_max_rounds=2),
    )
    result = await orch.run(task="一起做两件事", trace_id="t-unresolved")
    assert result.need_human_review is True
    assert "【critic 未通过】" in result.final_text
    assert expected_hint in result.final_text


# ── 专家失败信封识别 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("自动处理失败，已转人工处理", "我们自己的失败文案"),
        ("决策失败: connection refused", "决策层兜底文案"),
        (
            "Traceback (most recent call last):\n  File x\nRuntimeError: boom",
            "整条是异常",
        ),
        ("ValueError: 参数不对", "首行就是异常"),
        ("PR 审查需要配置 GITHUB_TOKEN；请设置后重试。", "缺凭据"),
        ("WEBHOOK_AUTH_TOKEN is not set", "缺凭据（英文）"),
    ],
)
def test_expert_failure_reason_recognizes_envelopes(text: str, why: str) -> None:
    assert expert_failure_reason(text) != ""


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        # 讨论代码的正常回答里提到异常名太常见，不能因此判失败
        "这段代码在入参为空时会抛 ValueError，建议先校验 urls 列表。",
        "RERANK_ENABLED 未配置时系统会退回 NoopReranker，保留 RRF 顺序。",
        "配置降级路径写在 app/config.py 里，未配置即关闭语义检索。",
        "已修复：给 pool.get 加了互斥锁，并在 finally 中关闭 ClientSession。",
    ],
)
def test_expert_failure_reason_does_not_cry_wolf(text: str) -> None:
    """误杀一条正常回答的代价远比漏认高：这些都必须放过。"""
    assert expert_failure_reason(text) == ""


@pytest.mark.asyncio
async def test_failure_envelope_short_circuits_to_human_not_critic() -> None:
    """失败被当成答案返回时，必须升级人工，而不是送进 Critic 评审。"""
    llm = StubLLM(lambda task: TWO_SUBTASKS)
    orch = CollabOrchestrator(
        collaboration_llm=llm,
        # expert 返回的是信封而非真答案
        experts={"coder": ScriptedExpert(reply="PR 审查需要配置 GITHUB_TOKEN；请设置后重试。")},
        settings_obj=_cfg(hitl_enabled=False),
    )
    result = await orch.run(task="审查这段代码", trace_id="t-envelope")
    assert result.status == "error"
    assert result.error_code == "agent_failed"
    assert result.need_human_review is True
    # 关键断言：Critic 一次都没被调用
    assert llm.critique_calls == 0


@pytest.mark.asyncio
async def test_failure_envelope_with_hitl_suspends_for_approval() -> None:
    """审批开着时，同一封失败信封应该挂起等人工，而不是以错误收口。"""
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts={"coder": ScriptedExpert(reply="自动处理失败，已转人工处理")},
        settings_obj=_cfg(hitl_enabled=True, default_role="operator"),
    )
    result = await orch.run(task="审查这段代码", trace_id="t-envelope-hitl")
    assert result.status == "pending_review"
    assert result.need_human_review is True
    assert result.error_code == ""


@pytest.mark.asyncio
async def test_genuine_answer_still_reaches_critic() -> None:
    """反向钉住：正常答案不能被这条规则误杀，照样要过评审。"""
    llm = StubLLM(lambda task: TWO_SUBTASKS)
    orch = CollabOrchestrator(
        collaboration_llm=llm,
        experts=_experts(),
        settings_obj=_cfg(),
    )
    result = await orch.run(task="写代码然后总结", trace_id="t-genuine")
    assert result.status == "ok"
    assert llm.critique_calls == 1
    assert result.error_code == ""


@pytest.mark.asyncio
async def test_two_subtasks_failing_together_does_not_break_the_graph() -> None:
    """多个子任务同时失败不能把图炸掉。

    ``execute_expert`` 是 Send 扇出的并发节点，两个 worker 在同一 superstep
    返回。``status`` / ``retry_reason`` / ``raw_output`` 都是 LastValue 通道，
    并发写会直接 InvalidUpdateError。第结果一版就是这么崩的，所以这条盯住：
    记账只能走带 reducer 的字段，升级必须留给单个节点决定。
    """
    from app.agents.retry import AgentExecutionFailed

    class AlwaysFails:
        async def execute(self, envelope):
            raise AgentExecutionFailed(attempts=1, last_error="boom")

    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts={"coder": AlwaysFails(), "review": AlwaysFails()},
        settings_obj=_cfg(hitl_enabled=False),
    )
    result = await orch.run(task="一起做两件事", trace_id="t-two-fail")
    assert result.status == "error"
    assert result.error_code == "agent_failed"
    assert result.need_human_review is True


@pytest.mark.asyncio
async def test_two_envelope_subtasks_together_does_not_break_the_graph() -> None:
    """两个子任务同时返回失败信封，同样不能炸图（同一类并发写）。"""
    reply = "PR 审查需要配置 GITHUB_TOKEN；请设置后重试。"
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts={"coder": ScriptedExpert(reply=reply), "review": ScriptedExpert(reply=reply)},
        settings_obj=_cfg(hitl_enabled=False),
    )
    result = await orch.run(task="一起做两件事", trace_id="t-two-envelope")
    assert result.status == "error"
    assert result.error_code == "agent_failed"


# ── 依赖感知调度 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dependent_subtask_never_starts_before_its_prerequisite() -> None:
    completed: set[int] = set()
    violations: list[str] = []

    class DependencyProbe:
        def __init__(self, name: str) -> None:
            self.name = name

        async def execute(self, envelope):
            slot = dict(envelope.agent_local_slot or {})
            index = int(slot["collab_subtask_index"])
            for dep in slot.get("collab_depends_on") or ():
                if int(dep) not in completed:
                    violations.append(f"{index} started before {dep}")
            if index == 0:
                await asyncio.sleep(0.05)
            completed.add(index)
            return f"{self.name}-{index}"

    from app.orchestration.collaboration import SubtaskAssignment

    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "先做根任务", "agent": "coder", "depends_on": []},
                {
                    "index": 1,
                    "instruction": "依赖根任务",
                    "agent": "general",
                    "depends_on": [0],
                },
            ]
        ),
        experts={
            "coder": DependencyProbe("coder"),
            "general": DependencyProbe("general"),
        },
        settings_obj=_cfg(),
    )

    result = await orch.run(task="先根后叶", trace_id="t-dependency-order")

    assert result.plan == (
        SubtaskAssignment(0, "先做根任务", "coder", ()),
        SubtaskAssignment(1, "依赖根任务", "general", (0,)),
    )
    assert violations == []


@pytest.mark.asyncio
async def test_dependent_subtask_receives_prerequisite_output() -> None:
    seen_slots: dict[int, dict] = {}

    class SlotRecordingExpert:
        async def execute(self, envelope):
            slot = dict(envelope.agent_local_slot or {})
            index = int(slot["collab_subtask_index"])
            seen_slots[index] = slot
            return "上游结论：使用 locking"

    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "分析", "agent": "coder", "depends_on": []},
                {
                    "index": 1,
                    "instruction": "根据分析写总结",
                    "agent": "general",
                    "depends_on": [0],
                },
            ]
        ),
        experts={"coder": SlotRecordingExpert(), "general": SlotRecordingExpert()},
        settings_obj=_cfg(),
    )

    await orch.run(task="分析后总结", trace_id="t-dependency-output")

    assert seen_slots[1]["collab_dependency_outputs"] == {
        "0": "上游结论：使用 locking"
    }


@pytest.mark.asyncio
async def test_dependency_cycle_fails_explicitly_instead_of_spinning() -> None:
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "A", "agent": "coder", "depends_on": [1]},
                {"index": 1, "instruction": "B", "agent": "general", "depends_on": [0]},
            ]
        ),
        experts=_experts(),
        settings_obj=_cfg(),
    )

    result = await orch.run(task="循环依赖", trace_id="t-dependency-cycle")

    assert result.status == "error"
    assert "循环依赖" in result.final_text
    assert result.node_path[-1] == "failed"


def test_workspace_path_must_stay_inside_the_configured_root(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(lambda task: TWO_SUBTASKS),
        experts=_experts(),
        settings_obj=_cfg(),
        workspace_root=allowed,
    )

    with pytest.raises(ValueError, match="越出工作区"):
        orch._workspace_state(str(outside), ())


@pytest.mark.asyncio
async def test_revise_reruns_target_and_all_downstream_dependents() -> None:
    rounds: dict[int, list[int]] = {0: [], 1: [], 2: []}

    class RoundRecordingExpert:
        async def execute(self, envelope):
            slot = dict(envelope.agent_local_slot or {})
            index = int(slot["collab_subtask_index"])
            rounds[index].append(int(slot["collab_round"]))
            return f"子任务 {index} 第 {slot['collab_round']} 轮"

    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "根任务", "agent": "coder", "depends_on": []},
                {
                    "index": 1,
                    "instruction": "下游任务",
                    "agent": "general",
                    "depends_on": [0],
                },
                {
                    "index": 2,
                    "instruction": "独立任务",
                    "agent": "coder",
                    "depends_on": [],
                },
            ],
            critiques=[
                {"decision": "revise", "reasons": ["根任务要改"], "targets": [0]},
                {"decision": "approve", "reasons": [], "targets": []},
            ],
        ),
        experts={"coder": RoundRecordingExpert(), "general": RoundRecordingExpert()},
        settings_obj=_cfg(),
    )

    result = await orch.run(task="根改则下游重跑", trace_id="t-dependency-revise")

    assert result.status == "ok"
    assert rounds[0] == [0, 1]
    assert rounds[1] == [0, 1]
    assert rounds[2] == [0]


@pytest.mark.asyncio
async def test_streamed_hitl_interrupt_frame_is_json_safe() -> None:
    events = []
    orch = CollabOrchestrator(
        collaboration_llm=StubLLM(
            lambda task: [
                {"index": 0, "instruction": "写点啥", "agent": "general", "depends_on": []}
            ]
        ),
        experts={"general": ScriptedExpert(reply="TODO: 需要人工复核")},
        settings_obj=_cfg(hitl_enabled=True),
    )

    async for event in orch.run_streamed(task="审查", trace_id="t-stream-hitl"):
        events.append(event)

    interrupt_events = [event for event in events if event.node == "__interrupt__"]
    assert len(interrupt_events) == 1
    payload = interrupt_events[0].update
    assert isinstance(payload, dict)
    assert payload["interrupts"][0]["kind"] == "collab_hitl"
    assert orch.finish("t-stream-hitl").status == "pending_review"


def test_litellm_collaboration_parser_accepts_js_like_model_json() -> None:
    assert LiteLLMCollaborationLLM._loads(
        "{decision: 'approve', reasons: ['ready'], targets: []}"
    ) == {
        "decision": "approve",
        "reasons": ["ready"],
        "targets": [],
    }
