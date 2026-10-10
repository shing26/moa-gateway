"""跑一次多 Agent 协作，把图里发生的事打印出来。

    uv run python scripts/run_collaboration.py "写一个 Python 函数，然后审查它，并且总结要点"
    uv run python scripts/run_collaboration.py --live "..."     # 用已配置的 LLM 做规划/评审
    uv run python scripts/run_collaboration.py --max-rounds 3 "..."

为什么是脚本而不是文档片段
--------------------------
"我用过 LangGraph"这句话的证据应该是**能跑的东西**，不是一段贴进博客的代码。
本脚本就是那个证据：默认 mock 模式零网络零 token 跑完整张图（plan → handoff →
专家并发执行 → critic 反思回边 → guard → HITL → deliver），把 ``node_path``
原样打出来——执行轨迹不是编的，是图真实走过的顺序。

``--live`` 走 ``COLLAB_LLM=litellm``：规划与评审换成已配置的 LLM，专家仍是
``AGENT_REGISTRY`` 里的真实 coder/general/review。它会真的花钱，所以不是默认。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.config  # noqa: F401  (side effect: load_dotenv())

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_TASK = "写一个 Python 函数，然后审查这段代码，并且总结一下要点"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跑一次多 Agent 协作链路")
    parser.add_argument("task", nargs="?", default=DEFAULT_TASK, help="要协作完成的任务")
    parser.add_argument("--live", action="store_true", help="用已配置的 LLM 做规划与评审")
    parser.add_argument("--max-rounds", type=int, default=None, help="覆盖 COLLAB_MAX_ROUNDS")
    parser.add_argument("--max-subtasks", type=int, default=None, help="覆盖 COLLAB_MAX_SUBTASKS")
    parser.add_argument("--trace-id", default="", help="固定 trace_id，便于复跑比对")
    return parser.parse_args(argv)


def _build(args: argparse.Namespace):
    from app.config import settings
    from app.orchestration.collaboration import (
        CollabOrchestrator,
        MockCollaborationLLM,
        ScriptedExpert,
    )

    if args.live:
        # 活体模式：专家用 registry 里的真 Agent（coder/general/review），
        # 它们各自会调已配置的 LLM。
        return CollabOrchestrator.from_deps()

    overrides = {}
    if args.max_rounds is not None:
        overrides["collab_max_rounds"] = args.max_rounds
    if args.max_subtasks is not None:
        overrides["collab_max_subtasks"] = args.max_subtasks
    class Cfg:
        collab_llm = "mock"
        collab_max_rounds = settings.collab_max_rounds
        collab_max_subtasks = settings.collab_max_subtasks
        # mock 演示刻意关掉审批：脚本是一次性命令，没有人会来点"批准"，
        # 挂在 interrupt 上只会让演示看起来像卡住了。
        hitl_enabled = False
        default_role = settings.default_role

    for key, value in overrides.items():
        setattr(Cfg, key, value)
    cfg = Cfg()
    # mock 模式：专家换成脚本质专家，保证零网络零 token 也能看到完整拓扑。
    return CollabOrchestrator(
        collaboration_llm=MockCollaborationLLM(max_subtasks=cfg.collab_max_subtasks),
        settings_obj=cfg,
        experts={key: ScriptedExpert() for key in ("coder", "general", "review")},
    )


async def _run(args: argparse.Namespace) -> int:
    orchestrator = _build(args)
    result = await orchestrator.run(
        task=args.task,
        trace_id=args.trace_id or None,
        session_id="collab-demo",
    )

    print(f"trace_id      : {result.trace_id}")
    print(f"task          : {result.task}")
    print(f"status        : {result.status}  (need_human_review={result.need_human_review})")
    print(f"rounds        : {result.rounds}   guard_action={result.guard_action or '-'}")
    print()
    print(f"plan ({len(result.plan)} subtasks)")
    for item in result.plan:
        deps = ",".join(str(d) for d in item.depends_on) or "-"
        print(f"  [{item.index}] {item.agent:<8} depends_on={deps:<6} {item.instruction}")
    print()
    print(f"per-agent outputs ({len(result.per_agent_outputs)})")
    for agent, text in result.per_agent_outputs.items():
        print(f"  {agent}: {text.replace(chr(10), ' | ')[:160]}")
    print()
    print(f"critique history ({len(result.critique_history)})")
    if not result.critique_history:
        print("  (none)")
    for verdict in result.critique_history:
        targets = ",".join(str(t) for t in verdict.targets) or "-"
        reasons = "; ".join(verdict.reasons) or "-"
        print(f"  {verdict.decision:<8} targets={targets:<8} reasons={reasons}")
    print()
    print(f"node_path ({len(result.node_path)})")
    for step in result.node_path:
        print(f"  {step}")
    print()
    print("final text")
    print(result.final_text or "(empty)")
    print()
    print(f"cost_usd={result.cost_usd}  llm_latency_ms={result.llm_latency_ms}  tool_calls={result.tool_calls}")
    if result.need_human_review:
        print("提示：状态为待复核，未通过人工确认前不要当作成功交付。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.live:
        os.environ.setdefault("COLLAB_LLM", "litellm")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
