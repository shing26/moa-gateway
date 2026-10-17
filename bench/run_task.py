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

from app.agents.provider import LLMClient, LLMConfig  # noqa: E402
from app.agents.tools import ToolRegistry  # noqa: E402

from bench.agent.loop import LLMBackedAgent  # noqa: E402
from bench.agent.types import Budget  # noqa: E402
from bench.harness.runner import (  # noqa: E402
    InvalidTaskError,
    load_task,
    run_task,
    summarize,
    write_result,
)
from bench.harness.workspace import MissingCommitError  # noqa: E402

logger = logging.getLogger("bench.run_task")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跑一个 bench 任务（真 LLM，花钱）")
    parser.add_argument("--task", required=True, help="任务 id，例如 988fccb")
    parser.add_argument("--tasks-dir", default=str(ROOT / "bench" / "tasks"))
    parser.add_argument("--workdir", default=str(ROOT / ".bench-work"))
    parser.add_argument("--out", default="", help="RunResult JSON 的落盘路径")
    parser.add_argument("--arm", default="L0")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-cost-usd", type=float, default=1.5)
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


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = parse_args(argv)
    spec = load_task(Path(args.tasks_dir) / f"{args.task}.task.json")
    llm = build_llm(args)

    def factory(*, workspace: Path, tools: ToolRegistry) -> LLMBackedAgent:
        return LLMBackedAgent(llm, tools)

    budget = Budget(max_cost_usd=args.max_cost_usd, max_steps=args.max_steps)
    try:
        result = await run_task(
            spec=spec,
            agent_factory=factory,
            repo=ROOT,
            workdir=Path(args.workdir),
            arm=args.arm,
            budget=budget,
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
