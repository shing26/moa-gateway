"""一条命令跑完一整轮剂量曲线：B1 主实验 → B3 放宽复跑 → 聚合报告（票 09）。

    .venv\\Scripts\\python.exe bench\\run_curve.py --tasks 988fccb a746b81 --repeats 3

**真花钱、不进 CI**：这里只是把票 09 的 runbook 做成一条可复跑的命令，省掉手敲
PowerShell 双层循环的口误。离线的那部分（编排、B3 选择、聚合）有单测覆盖，
用注入的假 runner 跑，不碰网络。

B3 不是"再造一批 run"，是**由 B1 的结果推出来的**：`plan_b3_reruns` 只挑 B1 里
`fail` / `budget_exhausted` 的 (任务, 档位)，并把该档在 B1 上的花费当基线带下去——
增量成本因此是推出来的，不是手填的（§5.3）。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# app.config 的副作用 load_dotenv()——与 bench/run_task.py 同一条，别绕过项目配置。
import app.config  # noqa: F401,E402  (side effect: load_dotenv())

from app.agents.provider import LLMClient, LLMConfig  # noqa: E402

from bench.agent.types import (  # noqa: E402
    B1_TIER,
    B3_TIER,
    AgentFactory,
    Budget,
    RunResult,
)
from bench.arms import DEFAULT_ARM, arm_names, resolve_arm  # noqa: E402
from bench.harness.budget import BudgetPolicy, plan_b3_reruns  # noqa: E402
from bench.harness.runner import (  # noqa: E402
    NEUTRAL_GOAL,
    InvalidTaskError,
    TaskSpec,
    load_task,
    run_task,
    write_result,
)
from bench.report import (  # noqa: E402
    DoseReport,
    build_report,
    load_rows_dir,
    render_markdown,
)

logger = logging.getLogger("bench.run_curve")

#: 跑一个任务的函数，默认就是 harness 的 ``run_task``；测试注入假的来离线演一遍。
RunTaskRunner = Callable[..., Awaitable[RunResult]]

#: 按档位名取 agent 工厂。
FactoryFor = Callable[[str], AgentFactory]


@dataclass(frozen=True)
class CurveOutcome:
    """一轮曲线的产物：两次落盘目录 + 两份聚合结论 + 被判无效的任务。"""

    b1_dir: Path
    b3_dir: Path
    b1_runs: int
    b3_runs: int
    b1_report: DoseReport
    b3_report: DoseReport
    invalid_tasks: tuple[str, ...] = ()


async def run_curve(
    *,
    specs: Sequence[TaskSpec],
    arms: Sequence[str],
    repeats: int,
    repo: Path,
    workdir: Path,
    b1_dir: Path,
    b3_dir: Path,
    factory_for: FactoryFor,
    runner: RunTaskRunner = run_task,
    policy: BudgetPolicy | None = None,
    goal: str = NEUTRAL_GOAL,
) -> CurveOutcome:
    """跑一轮 B1 + B3 并聚合。``runner`` 可注入，离线测试用它绕开真 LLM 与 git。"""
    if repeats < 1:
        raise ValueError(f"repeats 至少为 1，得到 {repeats}")
    policy = policy or BudgetPolicy()
    b1_dir.mkdir(parents=True, exist_ok=True)
    b3_dir.mkdir(parents=True, exist_ok=True)

    invalid: list[str] = []
    b1_results: list[RunResult] = []
    for spec in specs:
        task_failed_first = False
        for arm in arms:
            for repeat in range(1, repeats + 1):
                result = await _one(
                    runner=runner,
                    spec=spec,
                    arm=arm,
                    factory=factory_for(arm),
                    repo=repo,
                    workdir=workdir / f"b1-{spec.task_id}-{arm}-{repeat}",
                    budget=policy.b1(),
                    tier=B1_TIER,
                    baseline=0.0,
                    goal=goal,
                    out=b1_dir / f"{spec.task_id}-{arm}-{repeat}.json",
                    invalid=invalid,
                )
                if result is None:
                    # fail-first 没失败：整条任务无效，剩下的档位/重复不再浪费。
                    task_failed_first = True
                    break
                b1_results.append(result)
            if task_failed_first:
                break

    b3_results: list[RunResult] = []
    for plan in plan_b3_reruns(b1_results, policy=policy):
        for repeat in range(1, repeats + 1):
            result = await _one(
                runner=runner,
                spec=next(spec for spec in specs if spec.task_id == plan.task_id),
                arm=plan.arm,
                factory=factory_for(plan.arm),
                repo=repo,
                workdir=workdir / f"b3-{plan.task_id}-{plan.arm}-{repeat}",
                budget=plan.budget,
                tier=B3_TIER,
                baseline=plan.baseline_cost_usd,
                goal=goal,
                out=b3_dir / f"{plan.task_id}-{plan.arm}-{repeat}.json",
                invalid=invalid,
            )
            if result is not None:
                b3_results.append(result)

    declared = tuple(
        spec.task_id for spec in specs if spec.task_id not in set(invalid)
    )
    return CurveOutcome(
        b1_dir=b1_dir,
        b3_dir=b3_dir,
        b1_runs=len(b1_results),
        b3_runs=len(b3_results),
        b1_report=build_report(
            load_rows_dir(b1_dir), tier=B1_TIER, tasks=declared
        ),
        b3_report=build_report(
            load_rows_dir(b3_dir), tier=B3_TIER, tasks=declared
        ),
        invalid_tasks=tuple(sorted(set(invalid))),
    )


async def _one(
    *,
    runner: RunTaskRunner,
    spec: TaskSpec,
    arm: str,
    factory: AgentFactory,
    repo: Path,
    workdir: Path,
    budget: Budget,
    tier: str,
    baseline: float,
    goal: str,
    out: Path,
    invalid: list[str],
) -> RunResult | None:
    """跑一个 run 并落盘；任务被判无效（fail-first 没失败）时记下来返回 ``None``。"""
    try:
        result = await runner(
            spec=spec,
            agent_factory=factory,
            repo=repo,
            workdir=workdir,
            arm=arm,
            goal=goal,
            budget=budget,
            budget_tier=tier,
            baseline_cost_usd=baseline,
        )
    except InvalidTaskError as exc:
        logger.warning("任务无效，跳过：%s", exc)
        invalid.append(spec.task_id)
        return None
    write_result(result, out)
    logger.info(
        "%s arm=%s tier=%s repeat→%s outcome=%s",
        spec.task_id,
        arm,
        tier,
        out.name,
        result.outcome,
    )
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="跑一整轮剂量曲线（真 LLM，花钱；B1 主实验 + B3 放宽复跑 + 报告）"
    )
    parser.add_argument("--tasks", nargs="+", required=True, help="任务 id（可多条）")
    parser.add_argument("--tasks-dir", default=str(ROOT / "bench" / "tasks"))
    parser.add_argument(
        "--arms", nargs="+", default=list(arm_names()), help="档位，默认全部"
    )
    parser.add_argument("--repeats", type=int, default=3, help="每个 (任务 × 档位) 跑几次（§9：3）")
    parser.add_argument("--workdir", default=str(ROOT / ".bench-work" / "curve"))
    parser.add_argument(
        "--outdir",
        default="",
        help="落盘目录（默认 <workdir>/runs；B1 与 B3 分两个子目录）",
    )
    parser.add_argument("--main-cost-usd", type=float, default=None, help="覆盖 B1 美元上限")
    parser.add_argument("--main-steps", type=int, default=None, help="覆盖 B1 步数上限")
    parser.add_argument("--relaxed-cost-usd", type=float, default=None, help="覆盖 B3 美元上限")
    parser.add_argument("--relaxed-steps", type=int, default=None, help="覆盖 B3 步数上限")
    parser.add_argument("--max-tokens", type=int, default=None, help="覆盖 token 兜底上限")
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL")
    parser.add_argument("--temperature", type=float, default=None)
    return parser.parse_args(argv)


def _build_llm(args: argparse.Namespace) -> LLMClient:
    config = LLMConfig.from_env("LLM")
    if args.model:
        config = dataclasses.replace(config, model=args.model)
    if args.temperature is not None:
        config = dataclasses.replace(config, temperature=args.temperature)
    return LLMClient(config)


def _policy(args: argparse.Namespace) -> BudgetPolicy:
    policy = BudgetPolicy()
    overrides: dict[str, object] = {}
    for attr, flag in (
        ("main_cost_usd", "main_cost_usd"),
        ("main_steps", "main_steps"),
        ("relaxed_cost_usd", "relaxed_cost_usd"),
        ("relaxed_steps", "relaxed_steps"),
        ("max_tokens", "max_tokens"),
    ):
        value = getattr(args, flag)
        if value is not None:
            overrides[attr] = value
    return dataclasses.replace(policy, **overrides) if overrides else policy


def _unknown_arms(arms: Iterable[str]) -> tuple[str, ...]:
    known = set(arm_names())
    return tuple(name for name in arms if name not in known)


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _parse_args(argv)
    if bad := _unknown_arms(args.arms):
        # 静默漏掉一个档位会让曲线少一列却不报错——宁可这里就炸。
        logger.error("未知 arm：%s（可用：%s）", ", ".join(bad), "/".join(arm_names()))
        return 4

    specs = [
        load_task(Path(args.tasks_dir) / f"{task}.task.json") for task in args.tasks
    ]
    llm = _build_llm(args)
    outdir = Path(args.outdir) if args.outdir else Path(args.workdir) / "runs"
    outcome = await run_curve(
        specs=specs,
        arms=args.arms,
        repeats=args.repeats,
        repo=ROOT,
        workdir=Path(args.workdir),
        b1_dir=outdir / "b1",
        b3_dir=outdir / "b3",
        factory_for=lambda arm: resolve_arm(arm, llm=llm),
        policy=_policy(args),
    )
    print(f"B1 runs: {outcome.b1_runs}（{outcome.b1_dir}）")
    print(f"B3 runs: {outcome.b3_runs}（{outcome.b3_dir}）")
    if outcome.invalid_tasks:
        print(f"无效任务（fail-first 没失败）：{', '.join(outcome.invalid_tasks)}")
    for report in (outcome.b1_report, outcome.b3_report):
        print()
        print(render_markdown(report))
        out = Path(args.workdir) / f"report-{report.tier}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"聚合结果已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


__all__ = [
    "CurveOutcome",
    "FactoryFor",
    "RunTaskRunner",
    "main",
    "run_curve",
]
