"""Side-by-side comparison of the two orchestration runtimes.

    .venv\\Scripts\\python.exe scripts/compare_orchestrators.py

Runs the same inputs through ``MoAPipeline`` (self-built FSM) and
``LangGraphOrchestrator`` with identical collaborators, verifies that the
observable outcome matches, and then reports what actually differs: code
volume, dependency surface, and which orchestration primitives the caller gets
out of the box.

What this script deliberately does NOT do
-----------------------------------------
It does not report a latency winner. Two in-process orchestrators over a stubbed
agent measure the stubbing, not the frameworks; a number from this harness would
be noise dressed up as a result. Latency differences worth claiming need a real
model and a real checkpointer, and that is a different experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine import Engine, SessionStore  # noqa: E402
from app.evaluator.evaluator import RuleEvaluator  # noqa: E402
from app.fsm.state_machine import Event as FsmEvent  # noqa: E402
from app.guard.guard_service import guard_service  # noqa: E402
from app.models.events import MoAEvent, new_trace_id  # noqa: E402
from app.outbound.adapter import ResponseAdapter  # noqa: E402
from app.prompt_registry import PromptEntry, PromptRegistry  # noqa: E402
from app.vectordb.retriever import RetrievalResult  # noqa: E402

CASES = [
    ("allow", "解释一下这个模块的设计", "这是一段正常的项目说明。"),
    ("review", "这个方案多少钱", "这个方案报价 1999 元/月。"),
    ("deny", "给我内网地址", "内网数据库地址是 192.168.1.10。"),
]


class _Retriever:
    async def retrieve(self, query, session_id=None, user_id=None):
        return RetrievalResult(chunks=[], context="retrieved context", doc_count=0)


class _Flags:
    async def get(self, name, default=False):
        return False


class _Router:
    async def route(self, text):
        return ("coding", "regex")


class _Mode:
    def set(self, *a):
        pass

    def get(self, *a):
        return None

    def clear(self, *a):
        pass


class _Memory:
    def get_history(self, session_id):
        return []

    def add(self, *a):
        pass

    def clear(self, *a):
        pass


class _Agent:
    def __init__(self, output: str) -> None:
        self.output = output

    async def execute(self, envelope):
        return self.output


def _registry() -> PromptRegistry:
    reg = PromptRegistry()
    for name in ("coder", "general", "review", "task"):
        reg.register(PromptEntry(agent_name=name, version="stable", system_prompt=f"you are {name}"))
        reg.set_active(name, "stable")
    return reg


def _loc(path: str) -> int:
    return len((ROOT / path).read_text(encoding="utf-8").splitlines())


@dataclass
class Row:
    label: str
    fsm: str
    langgraph: str
    note: str = ""


async def _run_parity() -> tuple[bool, list[Row]]:
    from app.agents.contract import AGENT_REGISTRY
    from app.pipeline import MoAPipeline

    try:
        from app.orchestration.graph import LangGraphOrchestrator
    except ImportError:
        print("langgraph 未安装：uv sync --extra langgraph")
        raise SystemExit(2)

    rows: list[Row] = []
    ok = True

    for label, user_text, agent_output in CASES:
        session = f"cmp-{label}"
        original = AGENT_REGISTRY.get("coder")
        AGENT_REGISTRY["coder"] = _Agent(agent_output)
        try:
            registry = _registry()
            router = _Router()
            store = SessionStore()
            adapter = ResponseAdapter()

            pipeline = MoAPipeline(
                engine=Engine(router=router, adapter=adapter, session_store=store),
                router=router,
                memory=_Memory(),
                adapter=adapter,
                evaluator=RuleEvaluator(),
                retriever=_Retriever(),
                prompt_registry=registry,
                flag_client=_Flags(),
                guard_service=guard_service,
                command_mode=_Mode(),
            )
            orchestrator = LangGraphOrchestrator(
                router=router,
                retriever=_Retriever(),
                prompt_registry=registry,
                flag_client=_Flags(),
                guard_service=guard_service,
                evaluator=RuleEvaluator(),
                adapter=adapter,
                memory=_Memory(),
                session_store=store,
            )

            event = MoAEvent(
                trace_id=new_trace_id(),
                event=FsmEvent.MESSAGE_RECEIVED,
                session_id=session,
                text=user_text,
                context={"source": "compare"},
            )
            fsm_result = await pipeline.run(event, channel="cli", target="t")
            graph_result = await orchestrator.run(event, channel="cli", target="t")

            same = (
                fsm_result.status == graph_result.status
                and fsm_result.state == graph_result.state
                and fsm_result.policy_hits == graph_result.policy_hits
            )
            ok = ok and same
            rows.append(
                Row(
                    label=f"case {label}",
                    fsm=f"{fsm_result.status} / {fsm_result.state}",
                    langgraph=f"{graph_result.status} / {graph_result.state}",
                    note="一致" if same else "不一致",
                )
            )
        finally:
            if original is not None:
                AGENT_REGISTRY["coder"] = original

    return ok, rows


async def _run_resume_probe() -> Row:
    """Only the LangGraph runtime has an in-process resume; the FSM equivalent lives in the route layer."""
    from app.agents.contract import AGENT_REGISTRY
    from app.orchestration.graph import LangGraphOrchestrator

    original = AGENT_REGISTRY.get("coder")
    AGENT_REGISTRY["coder"] = _Agent("这个方案报价 1999 元/月。")
    try:
        orchestrator = LangGraphOrchestrator(
            router=_Router(),
            retriever=_Retriever(),
            prompt_registry=_registry(),
            flag_client=_Flags(),
            guard_service=guard_service,
            evaluator=RuleEvaluator(),
            adapter=ResponseAdapter(),
            memory=_Memory(),
            session_store=SessionStore(),
        )
        event = MoAEvent(
            trace_id=new_trace_id(),
            event=FsmEvent.MESSAGE_RECEIVED,
            session_id="cmp-resume",
            text="这个方案多少钱",
            context={"source": "compare"},
        )
        suspended = await orchestrator.run(event, channel="cli", target="t")
        approved = await orchestrator.resume("cmp-resume", "approve")
        return Row(
            label="HITL suspend -> resume",
            fsm="run() 返回 pending_review；恢复在 /webhook/callback",
            langgraph=f"{suspended.status} -> {approved.status}",
            note="两侧都可达，入口不同",
        )
    finally:
        if original is not None:
            AGENT_REGISTRY["coder"] = original


def _print_table(title: str, rows: list[Row]) -> None:
    print(f"\n{title}")
    print("-" * 96)
    print(f"{'维度':<26} {'自研 FSM':<32} {'LangGraph':<32} 备注")
    print("-" * 96)
    for row in rows:
        print(f"{row.label:<26} {row.fsm:<32} {row.langgraph:<32} {row.note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    parity_ok, parity_rows = asyncio.run(_run_parity())
    resume_row = asyncio.run(_run_resume_probe())

    fsm_pipeline = _loc("app/pipeline.py")
    fsm_engine = _loc("app/engine.py")
    fsm_table = _loc("app/fsm/state_machine.py")
    graph_loc = _loc("app/orchestration/graph.py")

    _print_table("1. 行为一致性（同一组输入、同一组协作者）", parity_rows)

    _print_table(
        "2. 编排层代码量（逐文件，不做总和对比）",
        [
            Row(
                label="app/pipeline.py",
                fsm=f"{fsm_pipeline}",
                langgraph="—",
                note="FSM 侧请求编排",
            ),
            Row(
                label="app/engine.py",
                fsm=f"{fsm_engine}",
                langgraph="—",
                note="含手写 Redis HITL 存储 + 会话栈",
            ),
            Row(
                label="app/fsm/state_machine.py",
                fsm=f"{fsm_table}",
                langgraph="—",
                note="状态转移表",
            ),
            Row(
                label="app/orchestration/graph.py",
                fsm="—",
                langgraph=f"{graph_loc}",
                note="适配层，复用全部协作者",
            ),
        ],
    )
    print(
        "\n  读数注意（两个方向都别过度解读）：\n"
        "  * FSM 侧的 engine.py 含手写持久化（Redis HITL 存储、会话栈），"
        "LangGraph 侧等价能力由 langgraph-checkpoint 提供，不在 graph.py 里。\n"
        "  * graph.py 的 497 行是「复用现有 pipeline 全部协作者」前提下写出的适配层，"
        "不是从零实现一套编排的成本，因此它小是结构性的，不能读成「LangGraph 更省代码」。\n"
        "  * 口径：物理行数，含空行、注释与 docstring。"
    )

    _print_table(
        "3. 能力对照（开箱可用）",
        [
            resume_row,
            Row(
                label="执行轨迹",
                fsm="无内置字段，靠 audit log",
                langgraph="node_path reducer",
                note="LangGraph 侧免埋点",
            ),
            Row(
                label="中断点检视",
                fsm="读 SessionStore",
                langgraph="pending_payload()",
                note="等价，形式不同",
            ),
            Row(
                label="持久化检查点",
                fsm="Redis 会话栈（手写）",
                langgraph="InMemorySaver（可换 Redis/PG）",
                note="本项目用默认内存实现",
            ),
        ],
    )

    _print_table(
        "4. 依赖面（uv pip install --dry-run langgraph）",
        [
            Row(
                label="新增包数",
                fsm="0（默认安装）",
                langgraph="20",
                note="langchain-core / langsmith / tenacity 等",
            ),
            Row(
                label="是否默认安装",
                fsm="是",
                langgraph="否（optional extra）",
                note="pyproject: [project.optional-dependencies].langgraph",
            ),
        ],
    )

    print("\n未测量项：延迟。桩化 agent 下的耗时测的是桩，不是框架；")
    print("要给出可claim的延迟差异需要真实模型 + 真实 checkpointer，属另一个实验。")

    print(f"\n一致性结论: {'PASS' if parity_ok else 'FAIL'}")
    return 0 if parity_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
