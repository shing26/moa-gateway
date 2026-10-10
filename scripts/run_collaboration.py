"""跑一次多 Agent 协作，把图里发生的事**实时**打印出来。

    uv run python scripts/run_collaboration.py
    uv run python scripts/run_collaboration.py --live "优化这段有并发竞态的异步代码"
    uv run python scripts/run_collaboration.py --full --max-rounds 3

为什么是脚本而不是文档片段
--------------------------
"我用过 LangGraph"这句话的证据应该是**能跑的东西**，不是一段贴进博客的代码。
默认 mock 模式零网络零 token 跑完整张图（plan → handoff → 专家并发执行 →
critic 反思回边 → guard → HITL → deliver）。

``--live`` 切到真实模型：``COLLAB_LLM=litellm`` 让 Supervisor/Critic 用已配置的
LLM 决策，``AGENT_LLM=litellm`` 让 coder/general/review 三个专家也走 LLM
（缺一个就会变成"规划很真、执行很假"的半活体）。它会真的花钱/花时间，所以不是
默认。配置从 ``.env.local`` 读——``app.config`` 的 ``load_dotenv()`` 只找 ``.env``。

流式是这么来的
--------------
``CollabOrchestrator.run_streamed()`` 用 ``graph.astream(stream_mode="updates")``，
每个 superstep 完成后 yield 一条事件。所以 Critic 打回是**当场**被打出来的，
不是跑完后从一段 JSON 里回读。最终结果另用 ``finish()`` 从 checkpoint 取——
``updates`` 给的是增量写入，不是完整状态。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from termui import erase_line, kv, paint, rule, wrap  # noqa: E402

DEFAULT_TASK = "写一个 Python 函数，然后审查这段代码，并且总结一下要点"

_STEP_MARK = {"plan": "①", "dispatch": "②", "critic": "③", "guard": "⑤"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跑一次多 Agent 协作链路")
    parser.add_argument("task", nargs="?", default=DEFAULT_TASK, help="要协作完成的任务")
    parser.add_argument("--live", action="store_true", help="用已配置的 LLM 跑真实链路")
    parser.add_argument(
        "--scripted-revise",
        action="store_true",
        help="mock 模式下演示一次 Critic 打回→定向重跑→放行（评审是预设的，不是模型说的）",
    )
    parser.add_argument("--full", action="store_true", help="专家输出不截断")
    parser.add_argument(
        "--scripted-failure",
        action="store_true",
        help="mock 模式下演示专家返回失败信封→Critic 不评审→升级人工",
    )
    parser.add_argument("--max-rounds", type=int, default=None, help="覆盖 COLLAB_MAX_ROUNDS")
    parser.add_argument("--max-subtasks", type=int, default=None, help="覆盖 COLLAB_MAX_SUBTASKS")
    parser.add_argument("--trace-id", default="", help="固定 trace_id，便于复跑比对")
    return parser.parse_args(argv)


def _prepare_live() -> None:
    """活体模式的前置：切 LLM 开关 + 读 ``.env.local``。必须发生在 import
    ``app.config`` 之前——``settings`` 是 import 期建的单例，事后改 environ 无效。
    """
    from dotenv import load_dotenv

    local = ROOT / ".env.local"
    if local.exists():
        load_dotenv(local, override=False)
    # 两个开关都要切：只切 COLLAB_LLM 会让方案设计走真模型、三个专家走 mock，
    # 看起来像活的，其实执行层是背好的台词。
    os.environ["COLLAB_LLM"] = "litellm"
    os.environ["AGENT_LLM"] = "litellm"


def _build(args: argparse.Namespace):
    from app.config import settings
    from app.orchestration.collaboration import (
        CollabOrchestrator,
        MockCollaborationLLM,
        ScriptedExpert,
    )

    if args.live:
        return CollabOrchestrator.from_deps()

    max_rounds = (
        args.max_rounds if args.max_rounds is not None else settings.collab_max_rounds
    )
    max_subtasks = (
        args.max_subtasks
        if args.max_subtasks is not None
        else settings.collab_max_subtasks
    )

    llm: Any = MockCollaborationLLM(max_subtasks=max_subtasks)
    if args.scripted_revise:
        llm = _ReviseOnceLLM()
    if args.scripted_failure:
        # 真实专家在依赖缺失时就是这么回答的：把失败当普通答案 return。
        return CollabOrchestrator(
            collaboration_llm=MockCollaborationLLM(max_subtasks=max_subtasks),
            settings_obj=_mock_cfg(max_rounds, max_subtasks, hitl_enabled=False),
            experts={
                key: ScriptedExpert(reply=_FAILURE_ENVELOPE)
                for key in ("coder", "general", "review")
            },
        )

    cfg = _mock_cfg(max_rounds, max_subtasks, hitl_enabled=False)
    return CollabOrchestrator(
        collaboration_llm=llm,
        settings_obj=cfg,
        experts={key: ScriptedExpert() for key in ("coder", "general", "review")},
    )


def _mock_cfg(max_rounds: int, max_subtasks: int, *, hitl_enabled: bool):
    from app.config import settings

    # 用 SimpleNamespace 而不是 class：class 体不参与闭包，在类体里写
    # ``hitl_enabled = hitl_enabled`` 引用不到外层参数（实测 NameError）。
    return types.SimpleNamespace(
        collab_llm="mock",
        collab_max_rounds=max_rounds,
        collab_max_subtasks=max_subtasks,
        # mock 演示刻意关掉审批：脚本是一次性命令，没有人会来点"批准"，
        # 挂在 interrupt 上只会让演示看起来像卡住了。
        hitl_enabled=hitl_enabled,
        default_role=settings.default_role,
    )


_FAILURE_ENVELOPE = "PR 审查需要配置 GITHUB_TOKEN；请设置后重试。"


class _ReviseOnceLLM:
    """第一轮评审固定打回子任务 0，第二轮放行。

    存在的理由很直接：真实小模型（本地 qwen2.5:7b）很可能第一轮就给 approve，
    那"Critic 打回 → 定向重跑"这条最有说服力的链路就永远看不见。有了它就能
    随时、确定地演示这条路径。

    **它是预设的台词，不是模型的判断**——脚本头部会打印这句提醒。评审逻辑本身
    （裁决结构、targets 定向重跑、回边、轮次计数）仍是图在跑。
    """

    _PLAN = [
        {
            "index": 0,
            "instruction": "审查这段异步代码，修复其中的并发竞态条件",
            "agent": "coder",
            "depends_on": [],
        },
        {
            "index": 1,
            "instruction": "核对连接池是否在使用后正确释放",
            "agent": "review",
            "depends_on": [0],
        },
    ]

    def __init__(self) -> None:
        from app.orchestration.collaboration import MockCollaborationLLM

        self._mock = MockCollaborationLLM()
        self._round = 0

    async def plan(self, *, task: str):
        return self._PLAN

    async def critique(self, **kwargs):
        self._round += 1
        if self._round == 1:
            return {
                "decision": "revise",
                "reasons": [
                    "修复只覆盖了单次读写路径，未处理并发场景下的竞态窗口",
                    "缺少互斥锁或等价的原子性保证",
                ],
                "targets": [0],
            }
        return {
            "decision": "approve",
            "reasons": ["第 2 轮补上了互斥锁，竞态窗口已关闭"],
            "targets": [],
        }


class _View:
    """把协作事件翻译成终端画面。"""

    def __init__(self, *, task: str, full: bool) -> None:
        self.task = task
        self.full = full
        self.round = 0
        self.pending: list[dict] = []
        self.seen_experts: set[int] = set()
        self.started = time.monotonic()
        self.expert_started: dict[int, float] = {}

    # ── 各节点 ─────────────────────────────────────────────────────────

    def on_plan(self, update: dict) -> None:
        plan = update.get("plan") or []
        self.round = int(update.get("round", 0) or 0)
        print(rule("① Supervisor 规划"))
        print(kv("  task", wrap(self.task, indent="      ", initial="")))
        print(paint(f"  拆出 {len(plan)} 个子任务：", "dim"))
        for item in plan:
            deps = ",".join(str(d) for d in item.get("depends_on") or []) or "-"
            head = (
                f"    [{item.get('index')}] @{item.get('agent'):<8} depends_on={deps}"
            )
            print(paint(head, "bold"))
            print(wrap(str(item.get("instruction") or ""), indent="          "))
        print()

    def on_dispatch(self, update: dict) -> None:
        self.pending = list(update.get("pending") or [])
        # dispatch 的 update 里没有 round（那只是节点写回的增量），沿用上一次
        # critic 留下的计数：0 = 还没评审过，即第一次执行。
        print(rule(f"② 第 {self.round + 1} 次执行（同一 superstep 并发）"))
        for item in self.pending:
            idx = int(item.get("index", -1))
            self.expert_started[idx] = time.monotonic()
            print(
                paint(f"  ⏳ 子任务{idx} → @{item.get('agent')}", "dim"),
                end="",
            )
            print(wrap(str(item.get("instruction") or "")[:70], indent="       ", initial="  "))
        print(paint(f"  ⏳ 等待 {len(self.pending)} 个专家返回", "dim"), end="\r", flush=True)

    def on_expert(self, update: dict) -> None:
        path = update.get("node_path") or []
        idx = -1
        for step in path:
            if ":" in step:
                idx = int(step.split(":", 1)[1])
                break
        if idx < 0 or idx in self.seen_experts:
            return
        self.seen_experts.add(idx)
        print(erase_line(), end="", flush=True)
        agent = next(
            (i.get("agent") for i in self.pending if int(i.get("index", -1)) == idx), "?"
        )
        elapsed = time.monotonic() - self.expert_started.get(idx, time.monotonic())
        outputs = update.get("outputs") or {}
        text = str(outputs.get(idx, "") or "").strip()
        failed = int(update.get("failure_envelopes", 0) or 0) > 0
        print(
            paint(
                f"  {'❌' if failed else '✅'} 子任务{idx} → @{agent} · {elapsed:.1f}s",
                "red" if failed else "green",
            )
        )
        if text:
            shown = text if self.full else text[:240] + ("…" if len(text) > 240 else "")
            print(wrap(shown, indent="       "))
        print()

    def on_critic(self, update: dict) -> None:
        verdict = update.get("verdict") or {}
        self.round = int(update.get("round", self.round) or 0)
        if update.get("retry_reason") or update.get("hitl_kind") == "failure_escalation":
            # 专家返回了失败信封：Critic 没有评审，图直接走升级腿。
            # 这里不能落到下面的 approve 分支——那会把"没评审"显示成"通过了"。
            # 不带轮次：这条分支里 critic 没有推进 round，显示的会是上一轮的编号。
            print(rule("③ Critic 未评审"))
            print(paint("  ⛔ 专家返回失败信封，升级人工", "bold", "red"))
            print(wrap(str(update.get("retry_reason") or ""), indent="     ", initial="     "))
            print(paint("     → 不评审：在错误提示上打转几轮都不会有结论", "dim"))
            print()
            return
        decision = str(verdict.get("decision") or "")
        print(rule(f"③ Critic 评审 · 第 {self.round} 轮"))
        if decision == "revise":
            print(paint("  ❌ REVISE  打回重做", "bold", "red"))
            reasons = verdict.get("reasons") or []
            targets = verdict.get("targets") or []
            if targets:
                print(paint(f"     targets  {list(targets)}", "yellow"))
            for reason in reasons:
                print(wrap(f"· {reason}", indent="     ", initial="     "))
            print(paint("     → 只重跑被打回的子任务，其余产出保留", "dim"))
        elif decision == "escalate":
            print(paint("  ⚠ ESCALATE  反思轮次用尽，升级人工", "bold", "yellow"))
            for reason in verdict.get("reasons") or []:
                print(wrap(f"· {reason}", indent="     ", initial="     "))
        else:
            print(paint("  ✅ APPROVED  放行", "bold", "green"))
            for reason in verdict.get("reasons") or []:
                print(wrap(f"· {reason}", indent="     ", initial="     "))
        print()

    def on_tail(self, node: str, update: dict) -> None:
        labels = {
            "summarize": "④ 汇总",
            "evaluate": "④ 规则评估",
            "guard": "⑤ 守卫判定",
            "prepare_hitl": "⑥ 挂起等待人工",
            "await_human": "⑥ 人工审批",
            "deliver": "⑦ 交付",
            "blocked": "⑦ 守卫拦截",
            "rejected": "⑦ 人工驳回",
            "failed": "⑦ 执行失败",
        }
        label = labels.get(node, node)
        if node == "guard":
            action = str(update.get("guard_action") or "")
            colour = {"allow": "green", "review": "yellow", "deny": "red"}.get(action, "dim")
            print(paint(f"  {label}  {action}", "bold", colour))
            reason = update.get("guard_reason")
            if reason:
                print(wrap(str(reason), indent="       ", initial="       "))
        elif node in ("deliver", "blocked", "rejected", "failed"):
            print(paint(f"  {label}  {update.get('status', '')}", "bold"))
            if update.get("error_code"):
                print(paint(f"       error_code={update['error_code']}", "red"))
        elif node == "evaluate":
            score = update.get("eval_score")
            issues = update.get("eval_issues") or []
            print(paint(f"  {label}  score={score} issues={len(issues)}", "bold"))
        else:
            print(paint(f"  {label}", "bold"))
        print()

    # ── 收尾 ───────────────────────────────────────────────────────────

    def panel(self, result) -> None:
        print(rule("协作结果"))
        print(kv("trace_id", result.trace_id))
        colour = {
            "ok": "green",
            "approved": "green",
            "pending_review": "yellow",
            "blocked": "red",
            "rejected": "red",
            "error": "red",
        }.get(result.status, "dim")
        print(kv("status", paint(result.status, "bold", colour)))
        print(kv("rounds", str(result.rounds)))
        print(kv("guard", result.guard_action or "-"))
        print(kv("cost", f"${result.cost_usd:.6f}"))
        print(kv("llm_latency", f"{result.llm_latency_ms:.0f} ms"))
        print(kv("tool_calls", str(result.tool_calls)))
        print(kv("elapsed", f"{time.monotonic() - self.started:.1f} s"))
        if result.need_human_review:
            print(kv("需人工", paint("是 —— 未通过人工确认前不要当作成功交付", "yellow")))
        path = list(result.node_path)
        print(paint("  图路径", "dim"))
        print(wrap("  →  ".join(path), indent="        ", initial="   "))
        print()
        print(paint("  final text", "bold"))
        print(wrap(result.final_text or "(empty)", indent="     ", initial="   "))
        print()


async def _run(args: argparse.Namespace) -> int:
    orchestrator = _build(args)
    view = _View(task=args.task, full=args.full)
    # trace_id 在这里就定下来：run_streamed 内部生成的话，finish() 无从知道该查
    # 哪个 checkpoint。updates 事件里不带它（那只是节点的增量写入）。
    from app.models.events import new_trace_id

    trace_id = args.trace_id or new_trace_id()

    print(rule("多 Agent 协作 · LangGraph StateGraph"))
    print(kv("task", args.task))
    if args.live:
        mode = paint("live（真实 LLM）", "bold", "magenta")
    elif args.scripted_revise:
        mode = paint("mock + 预设评审（打回是台词，不是模型说的）", "bold", "yellow")
    elif args.scripted_failure:
        mode = paint("mock + 预设失败信封（演示不评审直接升级）", "bold", "yellow")
    else:
        mode = "mock（零网络零 token）"
    print(kv("mode", mode))
    print()

    async for event in orchestrator.run_streamed(
        task=args.task, trace_id=trace_id, session_id="collab-demo"
    ):
        node = event.node
        update = event.update
        if node == "plan":
            view.on_plan(update)
        elif node == "dispatch":
            view.on_dispatch(update)
        elif node == "execute_expert":
            view.on_expert(update)
        elif node == "critic":
            view.on_critic(update)
        else:
            view.on_tail(node, update)

    result = orchestrator.finish(trace_id)
    view.panel(result)
    if result.need_human_review:
        print(paint("提示：状态为待复核，未通过人工确认前不要当作成功交付。", "yellow"))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.live:
        _prepare_live()
    # 只在此时 import app.config：settings 是 import 期单例，环境变量必须先就位。
    import app.config  # noqa: F401,E402

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
