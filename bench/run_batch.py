"""权威批量入口：按冻结协议执行可恢复的 Task × Arm × Repeat 矩阵。

默认 ``mock`` 模式只验证编排、落盘、恢复和报告，不访问网络。真实模型必须显式传
``--mode live``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.config  # noqa: F401,E402  (side effect: load_dotenv())

from app.agents.provider import LLMClient, LLMConfig  # noqa: E402
from bench.agent.types import Budget  # noqa: E402
from bench.arms import resolve_arm  # noqa: E402
from bench.experiment import (  # noqa: E402
    BatchRunner,
    DEFAULT_PROTOCOL_PATH,
    ExperimentSpec,
    ExperimentStore,
    TrialEvidence,
    load_protocol,
)
from bench.experiment.adapters import BenchExperimentAdapter  # noqa: E402
from bench.harness.budget import NO_COST_LIMIT  # noqa: E402
from bench.harness.runner import load_task, run_task  # noqa: E402

DEFAULT_OUT = ROOT / ".bench-experiments"


class MockBatchExecutor:
    """离线执行器：只证明矩阵、落盘与恢复，不冒充模型能力证据。"""

    async def execute(
        self, spec: ExperimentSpec, trial_index: int
    ) -> TrialEvidence:
        task_id = str(spec.config["task_id"])
        arm = str(spec.config["arm"])
        return TrialEvidence(
            status="pass",
            plan=({"agent": "mock", "depends_on": []},),
            node_path=("mock", "verify"),
            per_agent_outputs={"mock": f"{task_id}/{arm}/trial-{trial_index}"},
            final_text="mock evidence only",
            evidence={
                "mock": True,
                "task_id": task_id,
                "arm": arm,
                "budget_tier": str(spec.config.get("budget_tier", "B1")),
                "baseline_cost_usd": float(
                    spec.config.get("baseline_cost_usd", 0.0) or 0.0
                ),
                "baseline_tokens": int(
                    spec.config.get("baseline_tokens", 0) or 0
                ),
                "warning": "不得作为真实模型能力结论",
            },
        )


def _model_client(spec: ExperimentSpec) -> LLMClient:
    payload = spec.model
    provider = str(payload.get("provider", "local"))
    api_key = (
        os.getenv("COLLAB_EXPERIMENT_API_KEY", "")
        or os.getenv("LLM_API_KEY", "")
        or ("ollama" if provider == "local" else "")
    )
    return LLMClient(
        LLMConfig(
            api_key=api_key,
            base_url=str(payload.get("base_url", "http://localhost:11434/v1")),
            model=str(payload.get("model", "qwen2.5:3b")),
            provider=provider,
            temperature=float(payload.get("temperature", 0.0) or 0.0),
            timeout=float(payload.get("timeout", 120.0) or 120.0),
            max_tokens=int(payload.get("max_tokens", 1024) or 1024),
        )
    )


def _live_executor(spec: ExperimentSpec, *, output_root: Path):
    client = _model_client(spec)
    factory = resolve_arm(str(spec.config["arm"]), llm=client)
    task_path = ROOT / str(spec.config["tasks_dir"]) / (
        f"{spec.config['task_id']}.task.json"
    )
    task = load_task(task_path)
    budget_payload = dict(spec.config.get("budget") or {})
    max_cost = budget_payload.get("max_cost_usd")
    budget = Budget(
        max_cost_usd=(
            float(max_cost) if max_cost is not None else NO_COST_LIMIT
        ),
        max_steps=int(budget_payload["max_steps"]),
        max_tokens=int(budget_payload.get("max_tokens", 0) or 0),
    )

    async def run_one(current: ExperimentSpec, trial_index: int):
        workdir = (
            output_root
            / "_work"
            / str(current.config["batch_id"])
            / current.experiment_id
            / f"trial-{trial_index:03d}"
        )
        result = await run_task(
            spec=task,
            agent_factory=factory,
            repo=ROOT,
            workdir=workdir,
            arm=str(current.config["arm"]),
            budget=budget,
            budget_tier=str(current.config.get("budget_tier", "B1")),
            baseline_cost_usd=float(
                current.config.get("baseline_cost_usd", 0.0) or 0.0
            ),
            baseline_tokens=int(current.config.get("baseline_tokens", 0) or 0),
        )
        return result, workdir / task.task_id

    return BenchExperimentAdapter(run_one)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="可恢复的多智能体实验批次")
    sub = parser.add_subparsers(dest="command", required=True)
    batch = sub.add_parser("batch", help="执行一个协议矩阵批次")
    batch.add_argument("--protocol", default=str(DEFAULT_PROTOCOL_PATH))
    batch.add_argument(
        "--phase",
        choices=("pilot", "baseline", "formal"),
        default="pilot",
    )
    batch.add_argument("--tasks", nargs="+", default=None)
    batch.add_argument("--arms", nargs="+", default=None)
    batch.add_argument("--batch-id", default="")
    batch.add_argument("--mode", choices=("mock", "live"), default="mock")
    batch.add_argument("--out", default=str(DEFAULT_OUT))
    b3 = sub.add_parser("b3", help="从完整 B1 批次派生失败档位的 B3 放宽复跑")
    b3.add_argument("--protocol", default=str(DEFAULT_PROTOCOL_PATH))
    b3.add_argument("--source-batch", required=True)
    b3.add_argument(
        "--phase",
        choices=("pilot", "baseline", "formal"),
        default="formal",
    )
    b3.add_argument("--batch-id", default="")
    b3.add_argument("--mode", choices=("mock", "live"), default="mock")
    b3.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    protocol = load_protocol(args.protocol)
    store = ExperimentStore(args.out)
    output_root = Path(args.out).resolve()

    if args.mode == "mock":
        factory = lambda spec: MockBatchExecutor()
    else:
        factory = lambda spec: _live_executor(spec, output_root=output_root)

    outcome = await BatchRunner(store, protocol).run(
        phase=args.phase,
        batch_id=args.batch_id,
        task_ids=tuple(args.tasks) if args.tasks else None,
        arm_names=tuple(args.arms) if args.arms else None,
        executor_factory=factory,
    )
    payload: dict[str, Any] = outcome.report.to_dict()
    payload["manifest"] = str(outcome.manifest_path)
    payload["report_json"] = str(outcome.report_json_path)
    payload["report_markdown"] = str(outcome.report_markdown_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if outcome.manifest.status == "completed" else 1


async def _run_b3(args: argparse.Namespace) -> int:
    protocol = load_protocol(args.protocol)
    store = ExperimentStore(args.out)
    output_root = Path(args.out).resolve()

    if args.mode == "mock":
        factory = lambda spec: MockBatchExecutor()
    else:
        factory = lambda spec: _live_executor(spec, output_root=output_root)

    outcome = await BatchRunner(store, protocol).run_b3(
        source_batch_id=args.source_batch,
        phase=args.phase,
        batch_id=args.batch_id,
        executor_factory=factory,
    )
    payload: dict[str, Any] = outcome.report.to_dict()
    payload["manifest"] = str(outcome.manifest_path)
    payload["report_json"] = str(outcome.report_json_path)
    payload["report_markdown"] = str(outcome.report_markdown_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if outcome.manifest.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.command == "b3":
            return asyncio.run(_run_b3(args))
        return asyncio.run(_run(args))
    except (TypeError, ValueError, FileNotFoundError, FileExistsError) as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MockBatchExecutor", "main", "parse_args"]
