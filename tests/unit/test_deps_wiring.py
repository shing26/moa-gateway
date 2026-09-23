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


def test_init_feishu_tolerates_an_orchestrator_without_set_card_sender(monkeypatch) -> None:
    """ENGINE=langgraph 时 pipeline 是 EngineDispatcher，它没有 set_card_sender。

    回归（2026-09-23）：`init_feishu()` 此前直接 `pipeline.set_card_sender(...)`，
    于是"配了飞书凭据 + 切到 langgraph"会让**启动直接崩**（AttributeError）。
    图路径按设计不发卡片（见 graph.py 的 scope limits），所以正确行为是告警跳过，
    而不是把整个进程带下去。
    """
    from app import deps

    class _DispatcherLike:
        """只有 dispatcher 的方法，没有 set_card_sender。"""

        def describe(self) -> dict[str, str]:
            return {"engine": "langgraph"}

    monkeypatch.setattr(deps, "pipeline", _DispatcherLike())
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test-secret")

    deps.init_feishu()  # 不该抛
