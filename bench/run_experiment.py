"""可验证多智能体实验台 CLI。

    .venv\\Scripts\\python.exe bench/run_experiment.py collab \
        --task "读取 README 并总结" --trials 1 --mode mock

    .venv\\Scripts\\python.exe bench/run_experiment.py rerun \
        --manifest .bench-experiments/<experiment>/experiment.json \
        --compare-to .bench-experiments/<baseline>/experiment.json

协作与 bench arm 是不同执行适配器，但都写同一种 Experiment manifest 与 Run Record。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 保持与网关相同的一套 .env 加载语义。
import app.config  # noqa: F401,E402

from app.agents.provider import LLMClient, LLMConfig  # noqa: E402
from app.agents.stubs import CoderAgent, GeneralAgent  # noqa: E402
from app.config import settings  # noqa: E402
from app.orchestration.collaboration import (  # noqa: E402
    CollabOrchestrator,
    LiteLLMCollaborationLLM,
    MockCollaborationLLM,
    ScriptedExpert,
)
from bench.agent.types import Budget  # noqa: E402
from bench.arms import DEFAULT_ARM, resolve_arm  # noqa: E402
from bench.experiment import (  # noqa: E402
    BenchExperimentAdapter,
    CollaborationExperimentAdapter,
    ExperimentManifest,
    ExperimentRunner,
    ExperimentSpec,
    ExperimentStore,
    compare_run_records,
)
from bench.harness.runner import load_task, run_task  # noqa: E402

DEFAULT_OUT = ROOT / ".bench-experiments"


def _add_model_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--provider", default="local")
    parser.add_argument("--base-url", default="http://localhost:11434/v1")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-tokens", type=int, default=1024)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="多智能体实验台")
    sub = parser.add_subparsers(dest="command", required=True)

    collab = sub.add_parser("collab", help="跑业务协作适配器")
    collab.add_argument("--task", required=True)
    collab.add_argument("--trials", type=int, default=1)
    collab.add_argument("--out", default=str(DEFAULT_OUT))
    collab.add_argument("--experiment-id", default="")
    collab.add_argument("--mode", choices=("mock", "live"), default="mock")
    collab.add_argument("--workspace", default="")
    collab.add_argument(
        "--verify-command-json",
        default="[]",
        help='固定验证命令的 JSON 数组，例如 \'["python","-m","pytest","-q"]\'',
    )
    collab.add_argument("--max-rounds", type=int, default=2)
    collab.add_argument("--max-subtasks", type=int, default=4)
    collab.add_argument("--hitl", action="store_true")
    collab.add_argument("--compare-to", default="")
    _add_model_args(collab)

    bench = sub.add_parser("bench", help="跑 bench L0-L3 适配器")
    bench.add_argument("--task-id", required=True)
    bench.add_argument("--tasks-dir", default=str(ROOT / "bench" / "tasks"))
    bench.add_argument("--arm", default=DEFAULT_ARM)
    bench.add_argument("--trials", type=int, default=1)
    bench.add_argument("--out", default=str(DEFAULT_OUT))
    bench.add_argument("--experiment-id", default="")
    bench.add_argument("--budget-tier", default="B1")
    bench.add_argument("--max-cost-usd", type=float, default=1.0)
    bench.add_argument("--max-steps", type=int, default=20)
    bench.add_argument("--budget-tokens", type=int, default=0)
    bench.add_argument("--compare-to", default="")
    _add_model_args(bench)

    rerun = sub.add_parser("rerun", help="从 manifest 受控复跑")
    rerun.add_argument("--manifest", required=True)
    rerun.add_argument("--trials", type=int, default=0, help="0 = 沿用原 Trial 数")
    rerun.add_argument("--out", default="")
    rerun.add_argument("--experiment-id", default="")
    rerun.add_argument("--compare-to", default="")
    return parser.parse_args(argv)


def _model_config(args: argparse.Namespace) -> tuple[LLMClient, dict[str, Any]]:
    api_key = (
        os.getenv("COLLAB_EXPERIMENT_API_KEY", "")
        or os.getenv("LLM_API_KEY", "")
        or ("ollama" if args.provider == "local" else "")
    )
    config = LLMConfig(
        api_key=api_key,
        base_url=args.base_url,
        model=args.model,
        provider=args.provider,
        temperature=args.temperature,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
    )
    recorded = {
        "provider": args.provider,
        "base_url": args.base_url,
        "model": args.model,
        "temperature": args.temperature,
        "timeout": args.timeout,
        "max_tokens": args.max_tokens,
    }
    return LLMClient(config), recorded


def _collab_spec(args: argparse.Namespace) -> tuple[ExperimentSpec, Any]:
    workspace = str(Path(args.workspace).resolve()) if args.workspace else ""
    try:
        raw_verify = json.loads(args.verify_command_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"--verify-command-json 不是合法 JSON: {exc}") from exc
    if not isinstance(raw_verify, list):
        raise TypeError("--verify-command-json 必须是字符串数组")
    if not all(isinstance(token, str) for token in raw_verify):
        raise TypeError("--verify-command-json 的每一项都必须是字符串")
    verify_command = tuple(raw_verify)
    model: dict[str, Any] = {}
    usage_source = None
    if args.mode == "live":
        client, model = _model_config(args)
        collaboration_llm = LiteLLMCollaborationLLM(client=client)
        experts = {
            "coder": CoderAgent(llm=client),
            "general": GeneralAgent(llm=client),
        }
        usage_source = client
    else:
        collaboration_llm = MockCollaborationLLM(
            max_subtasks=max(1, args.max_subtasks)
        )
        experts = {key: ScriptedExpert() for key in ("coder", "general", "review")}

    cfg = types.SimpleNamespace(
        collab_llm=args.mode,
        collab_max_rounds=max(0, args.max_rounds),
        collab_max_subtasks=max(1, args.max_subtasks),
        collab_workspace_root=workspace,
        hitl_enabled=bool(args.hitl),
        default_role=settings.default_role,
    )
    orchestrator = CollabOrchestrator(
        collaboration_llm=collaboration_llm,
        experts=experts,
        settings_obj=cfg,
        workspace_root=workspace or None,
    )
    spec = ExperimentSpec(
        experiment_id=args.experiment_id,
        task=args.task,
        adapter="collaboration",
        trials=max(1, args.trials),
        workspace_ref=workspace,
        verify_command=verify_command,
        model=model,
        config={
            "mode": args.mode,
            "max_rounds": args.max_rounds,
            "max_subtasks": args.max_subtasks,
            "hitl_enabled": bool(args.hitl),
            "prompt_version": "collaboration-prompts-v1",
        },
        metadata={"entrypoint": "bench/run_experiment.py"},
    )
    executor = CollaborationExperimentAdapter(
        orchestrator=orchestrator,
        usage_source=usage_source,
    )
    return spec, executor


def _bench_spec(args: argparse.Namespace) -> tuple[ExperimentSpec, Any]:
    _, model = _model_config(args)
    spec = ExperimentSpec(
        experiment_id=args.experiment_id,
        task=f"bench:{args.task_id}",
        adapter=f"bench:{args.arm}",
        trials=max(1, args.trials),
        model=model,
        config={
            "task_id": args.task_id,
            "tasks_dir": str(Path(args.tasks_dir).resolve()),
            "arm": args.arm,
            "budget_tier": args.budget_tier,
            "max_cost_usd": args.max_cost_usd,
            "max_steps": args.max_steps,
            "max_tokens": args.budget_tokens,
            "prompt_version": "bench-arm-prompts-v1",
        },
        metadata={"entrypoint": "bench/run_experiment.py"},
    )

    async def run_one(current: ExperimentSpec, trial_index: int):
        cfg = current.config
        task_spec = load_task(
            Path(str(cfg["tasks_dir"])) / f"{cfg['task_id']}.task.json"
        )
        client, _ = _model_config_from_record(current.model)
        factory = resolve_arm(str(cfg["arm"]), llm=client)
        # CLI 的 store 根由调用方传入；这里通过 metadata 注入实际输出根。
        output_root = Path(str(current.metadata.get("output_root", DEFAULT_OUT)))
        workdir = output_root / "_work" / current.experiment_id / f"trial-{trial_index:03d}"
        budget = Budget(
            max_cost_usd=float(cfg["max_cost_usd"]),
            max_steps=int(cfg["max_steps"]),
            max_tokens=int(cfg.get("max_tokens", 0) or 0),
        )
        result = await run_task(
            spec=task_spec,
            agent_factory=factory,
            repo=ROOT,
            workdir=workdir,
            arm=str(cfg["arm"]),
            budget=budget,
            budget_tier=str(cfg["budget_tier"]),
        )
        return result, workdir / task_spec.task_id

    return spec, BenchExperimentAdapter(run_one)


def _model_config_from_record(payload: dict[str, Any]) -> tuple[LLMClient, dict[str, Any]]:
    provider = str(payload.get("provider", "local"))
    base_url = str(payload.get("base_url", "http://localhost:11434/v1"))
    model = str(payload.get("model", "qwen2.5:3b"))
    api_key = (
        os.getenv("COLLAB_EXPERIMENT_API_KEY", "")
        or os.getenv("LLM_API_KEY", "")
        or ("ollama" if provider == "local" else "")
    )
    config = LLMConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        provider=provider,
        temperature=float(payload.get("temperature", 0.0) or 0.0),
        timeout=float(payload.get("timeout", 120.0) or 120.0),
        max_tokens=int(payload.get("max_tokens", 1024) or 1024),
    )
    return LLMClient(config), dict(payload)


def _load_manifest(path: Path) -> ExperimentManifest:
    return ExperimentManifest.from_dict(
        json.loads(path.read_text(encoding="utf-8")), path=path
    )


def _rerun_spec(manifest: ExperimentManifest, args: argparse.Namespace) -> ExperimentSpec:
    spec = replace(
        manifest.spec,
        experiment_id=args.experiment_id,
        trials=(args.trials if args.trials > 0 else manifest.spec.trials),
        parent_experiment_id=manifest.experiment_id,
    )
    if spec.adapter.startswith("bench:"):
        spec = replace(
            spec,
            metadata={**spec.metadata, "output_root": args.out or str(manifest.path.parent.parent)},
        )
    return spec


def _executor_for_spec(spec: ExperimentSpec) -> Any:
    if spec.adapter == "collaboration":
        raise ValueError("rerun collaboration 需要先从 manifest 重建 live/mock 执行器")
    if spec.adapter.startswith("bench:"):
        args = types.SimpleNamespace(
            task_id=spec.config["task_id"],
            tasks_dir=spec.config["tasks_dir"],
            arm=spec.config["arm"],
            trials=spec.trials,
            experiment_id=spec.experiment_id,
            budget_tier=spec.config.get("budget_tier", "B1"),
            max_cost_usd=spec.config.get("max_cost_usd", 1.0),
            max_steps=spec.config.get("max_steps", 20),
            budget_tokens=spec.config.get("max_tokens", 0),
            model=spec.model.get("model", "qwen2.5:3b"),
            provider=spec.model.get("provider", "local"),
            base_url=spec.model.get("base_url", "http://localhost:11434/v1"),
            temperature=spec.model.get("temperature", 0.0),
            timeout=spec.model.get("timeout", 120.0),
            out=str(spec.metadata.get("output_root", DEFAULT_OUT)),
        )
        spec, executor = _bench_spec(args)
        spec = replace(
            spec,
            experiment_id=args.experiment_id,
            trials=args.trials,
            parent_experiment_id=str(
                spec.model.get("parent_experiment_id", "") or ""
            ),
            metadata={**spec.metadata, "output_root": args.out},
        )
        return spec, executor
    raise ValueError(f"未知实验适配器: {spec.adapter}")


def _compare(
    baseline_manifest: Path,
    candidate: Any,
) -> list[dict[str, Any]]:
    store = ExperimentStore(baseline_manifest.parent.parent)
    baseline = store.load_experiment_records(baseline_manifest.parent.name)
    comparisons: list[dict[str, Any]] = []
    if len(baseline) != len(candidate.records):
        comparisons.append(
            {
                "baseline_trial": "",
                "candidate_trial": "",
                "divergences": ["trial_count"],
            }
        )
    for left, right in zip(baseline, candidate.records, strict=False):
        comparisons.append(
            {
                "baseline_trial": left.trial_id,
                "candidate_trial": right.trial_id,
                "divergences": compare_run_records(left, right),
            }
        )
    return comparisons


def _summary(outcome: Any, comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "experiment_id": outcome.manifest.experiment_id,
        "manifest": str(outcome.manifest_path),
        "adapter": outcome.manifest.spec.adapter,
        "trial_count": outcome.manifest.trial_count,
        "status_counts": outcome.manifest.status_counts,
        "records": [
            {
                "trial_id": record.trial_id,
                "status": record.status,
                "duration_ms": record.duration_ms,
                "metrics": record.metrics.to_dict(),
                "path": str(record.path),
            }
            for record in outcome.records
        ],
        "comparisons": comparisons,
    }


async def _run_command(args: argparse.Namespace) -> int:
    if args.command == "collab":
        spec, executor = _collab_spec(args)
        store = ExperimentStore(args.out)
    elif args.command == "bench":
        spec, executor = _bench_spec(args)
        spec = replace(
            spec,
            metadata={**spec.metadata, "output_root": str(Path(args.out).resolve())},
        )
        store = ExperimentStore(args.out)
    else:
        manifest = _load_manifest(Path(args.manifest))
        spec = _rerun_spec(manifest, args)
        if spec.adapter == "collaboration":
            mode = str(spec.config.get("mode", "mock"))
            collab_args = types.SimpleNamespace(
                task=spec.task,
                trials=spec.trials,
                out=args.out or str(manifest.path.parent.parent),
                experiment_id=spec.experiment_id,
                mode=mode,
                workspace=spec.workspace_ref,
                verify_command_json=json.dumps(
                    list(spec.verify_command), ensure_ascii=False
                ),
                max_rounds=int(spec.config.get("max_rounds", 2)),
                max_subtasks=int(spec.config.get("max_subtasks", 4)),
                hitl=bool(spec.config.get("hitl_enabled", False)),
                model=spec.model.get("model", "qwen2.5:3b"),
                provider=spec.model.get("provider", "local"),
                base_url=spec.model.get("base_url", "http://localhost:11434/v1"),
                temperature=spec.model.get("temperature", 0.0),
                timeout=spec.model.get("timeout", 120.0),
                max_tokens=spec.model.get("max_tokens", 1024),
            )
            _, executor = _collab_spec(collab_args)
            spec = replace(spec, parent_experiment_id=manifest.experiment_id)
            store = ExperimentStore(args.out or str(manifest.path.parent.parent))
        else:
            spec, executor = _executor_for_spec(spec)
            spec = replace(spec, parent_experiment_id=manifest.experiment_id)
            store = ExperimentStore(args.out or str(manifest.path.parent.parent))

    outcome = await ExperimentRunner(store).run(spec, executor)
    comparisons = (
        _compare(Path(args.compare_to), outcome)
        if getattr(args, "compare_to", "")
        else []
    )
    print(json.dumps(_summary(outcome, comparisons), ensure_ascii=False, indent=2))
    return 0 if outcome.records and all(
        record.status != "error" for record in outcome.records
    ) else 1


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run_command(parse_args(argv)))
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
