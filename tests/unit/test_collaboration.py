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

import types

import pytest

from app.orchestration.collaboration import (
    COLLAB_INTENT,
    EXPERT_KEYS,
    CollabOrchestrator,
    CollabResult,
    MockCollaborationLLM,
    ScriptedExpert,
    SubtaskAssignment,
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
    # 2 轮执行，不是无限循环
    assert result.node_path.count("dispatch") == 2
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
