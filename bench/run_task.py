"""手动跑一个 bench 任务：真 LLM、真花钱、真跑测试。

    .venv\\Scripts\\python.exe bench/run_task.py --task 988fccb

**不进 CI**：CI 只跑 ``tests/unit`` 里那批离线确定性的用例。这里跑的是 §5.0 的
人工验收——要么把测试从红跑成绿，要么诚实地卡住，两种情况都会落一份完整
``RunResult`` JSON。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 导入 app.config 是为了它的副作用 load_dotenv()：不先加载 .env，LLMConfig.from_env
# 会退回默认值（provider=direct / gpt-4o-mini / api.openai.com），于是 CLI 拿默认模型
# 去连 OpenAI——本项目其它脚本（scripts/compare_rerank.py 等）走的就是这一条。
import app.config  # noqa: F401  (side effect: load_dotenv())

from app.agents.provider import LLMClient, LLMConfig  # noqa: E402

from bench.agent.types import Budget  # noqa: E402
from bench.arms import (  # noqa: E402
    DEFAULT_ARM,
    UnknownArmError,
    arm_names,
    available_arms,
    resolve_arm,
)
from bench.harness.budget import B1_TIER, B3_TIER, BudgetPolicy  # noqa: E402
from bench.harness.runner import (  # noqa: E402
    InvalidTaskError,
    load_task,
    run_task,
    summarize,
    write_result,
)
from bench.harness.workspace import MissingCommitError  # noqa: E402

logger = logging.getLogger("bench.run_task")


def _arm_help() -> str:
    """档位说明直接来自注册表——CLI 与测试读的是同一份"哪些可用"。"""
    listing = "；".join(f"{arm.name} = {arm.description}" for arm in available_arms())
    return f"档位，默认 {DEFAULT_ARM}：{listing}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跑一个 bench 任务（真 LLM，花钱）")
    parser.add_argument("--task", required=True, help="任务 id，例如 988fccb")
    parser.add_argument("--tasks-dir", default=str(ROOT / "bench" / "tasks"))
    parser.add_argument("--workdir", default=str(ROOT / ".bench-work"))
    parser.add_argument("--out", default="", help="RunResult JSON 的落盘路径")
    parser.add_argument("--arm", default=DEFAULT_ARM, help=_arm_help())
    parser.add_argument(
        "--budget-tier",
        choices=[B1_TIER, B3_TIER],
        default=B1_TIER,
        help="B1 = 等预算主实验（四档共用上限）；B3 = 放宽复跑（只给 B1 失败的档位）",
    )
    parser.add_argument(
        "--baseline-cost-usd",
        type=float,
        default=0.0,
        help="B3 复跑时同 (task, arm) 的 B1 花费，用于算增量成本",
    )
    parser.add_argument("--max-steps", type=int, default=None, help="覆盖该档默认步数上限")
    parser.add_argument(
        "--max-cost-usd", type=float, default=None, help="覆盖该档默认美元上限"
    )
    parser.add_argument("--model", default="", help="覆盖 LLM_MODEL（用 LiteLLM 的限定名）")
    parser.add_argument("--temperature", type=float, default=None)
    return parser.parse_args(argv)


def build_llm(args: argparse.Namespace) -> LLMClient:
    config = LLMConfig.from_env("LLM")
    if args.model:
        config = dataclasses.replace(config, model=args.model)
    if args.temperature is not None:
        config = dataclasses.replace(config, temperature=args.temperature)
    return LLMClient(config)


def _resolve_budget(args: argparse.Namespace) -> Budget:
    """预算来自档位策略（B1/B3 的单一来源），``--max-*`` 只做显式覆盖。"""
    budget = BudgetPolicy().for_tier(args.budget_tier)
    if args.max_cost_usd is not None:
        budget = dataclasses.replace(budget, max_cost_usd=args.max_cost_usd)
    if args.max_steps is not None:
        budget = dataclasses.replace(budget, max_steps=args.max_steps)
    return budget


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = parse_args(argv)
    llm = build_llm(args)
    try:
        agent_factory = resolve_arm(args.arm, llm=llm)
    except UnknownArmError as exc:
        # 静默退回 L0 会让"曲线上的 L3 其实是 L0"无声通过——宁可这里就炸。
        logger.error("未知 arm：%s（可用：%s）", exc.name, "/".join(arm_names()) or "无")
        return 4
    spec = load_task(Path(args.tasks_dir) / f"{args.task}.task.json")

    budget = _resolve_budget(args)
    try:
        result = await run_task(
            spec=spec,
            agent_factory=agent_factory,
            repo=ROOT,
            workdir=Path(args.workdir),
            arm=args.arm,
            budget=budget,
            budget_tier=args.budget_tier,
            baseline_cost_usd=args.baseline_cost_usd,
        )
    except MissingCommitError as exc:
        logger.error("基线提交不可用：%s（需要完整克隆，浅克隆拿不到历史）", exc)
        return 2
    except InvalidTaskError as exc:
        logger.error("任务无效：%s", exc)
        return 3

    print(summarize(result))
    out = Path(args.out) if args.out else Path(args.workdir) / f"{spec.task_id}.result.json"
    print(f"结果已写入: {write_result(result, out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
