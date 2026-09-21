"""组合根接线：deps 构造的协作对象必须真的传给两个引擎（同一实例）。

背景（2026-09-20 评审发现）：M6 的 budget_guard 在 deps 里构造了、却没传给
MoAPipeline——默认引擎 FSM 路径上的预算拦截因此是"死"的，只有 LangGraph
路径通过 from_deps 拿到。参数默认 None 的写法让这种漏接静默通过，故用
"同一实例"断言把它钉进 CI（与 test_langgraph_adapter 的协作对象门禁同源）。
"""

from __future__ import annotations

import pytest


def test_fsm_pipeline_shares_composition_root_singletons() -> None:
    from app import deps

    assert deps.fsm_pipeline.budget_guard is deps.budget_guard, (
        "budget_guard 未接到 FSM 管道：默认引擎上的预算拦截会静默失效"
    )
    assert deps.fsm_pipeline.context_budget is deps.context_budget, (
        "context_budget 未接到 FSM 管道：上下文预算会被静默禁用"
    )
    assert deps.fsm_pipeline.long_term_memory is deps.long_term_memory


def test_selected_pipeline_is_fsm_by_default() -> None:
    from app import deps

    # 默认 ENGINE=fsm：路由拿到的就是同一个 fsm_pipeline 实例
    assert deps.pipeline is deps.fsm_pipeline


def test_langgraph_orchestrator_gets_the_same_instances() -> None:
    pytest.importorskip("langgraph", reason="optional extra: uv sync --extra langgraph")
    from app import deps
    from app.orchestration.graph import LangGraphOrchestrator

    graph = LangGraphOrchestrator.from_deps()
    # 私有属性是过渡约定；"同一实例"是 ADR-008 的等价性承诺本身
    assert graph._budget_guard is deps.budget_guard
    assert graph._context_budget is deps.context_budget
    assert graph._long_term_memory is deps.long_term_memory
