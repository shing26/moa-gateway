"""把业务协作图与 bench arm 的既有结果映射为统一 Run Record 证据。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bench.experiment.types import (
    ExperimentSpec,
    RunMetrics,
    TrialEvidence,
    WorkspaceSnapshot,
    fingerprint_workspace,
)

_EVIDENCE_TEXT_LIMIT = 4_000


def _usage_metrics(usage: dict[str, Any] | None) -> RunMetrics:
    data = usage or {}
    return RunMetrics(
        llm_calls=int(data.get("llm_calls", 0) or 0),
        prompt_tokens=int(data.get("prompt_tokens", 0) or 0),
        completion_tokens=int(data.get("completion_tokens", 0) or 0),
        cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
        llm_latency_ms=float(data.get("llm_latency_ms", 0.0) or 0.0),
    )


def _argument_evidence(arguments: dict[str, Any]) -> dict[str, Any]:
    """保留可复核参数形状，但不把整份写入正文塞进 Run Record。"""
    result: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and (
            key == "content" or len(value) > _EVIDENCE_TEXT_LIMIT
        ):
            result[key] = {
                "chars": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "truncated": True,
            }
        elif isinstance(value, str):
            result[key] = value[:_EVIDENCE_TEXT_LIMIT]
        else:
            result[key] = value
    return result


def _step_evidence(step: Any) -> dict[str, Any]:
    payload = asdict(step)
    payload["arguments"] = _argument_evidence(dict(step.arguments))
    payload["observation"] = str(step.observation or "")[:_EVIDENCE_TEXT_LIMIT]
    return payload


class CollaborationExperimentAdapter:
    """执行一次真实业务协作，并保留治理与 DAG 证据。"""

    def __init__(self, *, orchestrator: Any, usage_source: Any = None) -> None:
        self._orchestrator = orchestrator
        self._usage_source = usage_source

    async def execute(self, spec: ExperimentSpec, trial_index: int) -> TrialEvidence:
        before = self._snapshot(spec.workspace_ref)
        if self._usage_source is not None and hasattr(self._usage_source, "reset_usage"):
            self._usage_source.reset_usage()

        trial_id = f"{spec.experiment_id}-trial-{trial_index:03d}"
        result = await self._orchestrator.run(
            task=spec.task,
            trace_id=trial_id,
            session_id=trial_id,
            workspace_path=spec.workspace_ref,
            verify_command=spec.verify_command,
        )
        after = self._snapshot(spec.workspace_ref)
        usage = dict(getattr(self._usage_source, "usage_totals", {}) or {})
        metrics = _usage_metrics(usage)
        metrics = RunMetrics(
            llm_calls=metrics.llm_calls,
            prompt_tokens=metrics.prompt_tokens,
            completion_tokens=metrics.completion_tokens,
            cost_usd=(
                metrics.cost_usd
                if metrics.llm_calls
                else float(result.cost_usd or 0.0)
            ),
            llm_latency_ms=(
                metrics.llm_latency_ms
                if metrics.llm_calls
                else float(result.llm_latency_ms or 0.0)
            ),
            tool_calls=int(result.tool_calls),
            tool_errors=int(getattr(result, "tool_errors", 0) or 0),
            tool_arg_rejections=int(getattr(result, "tool_arg_rejections", 0) or 0),
        )
        return TrialEvidence(
            status=str(result.status),
            plan=tuple(item.to_dict() for item in result.plan),
            node_path=tuple(result.node_path),
            per_agent_outputs=dict(result.per_agent_outputs),
            final_text=str(result.final_text),
            metrics=metrics,
            prompt_hashes=tuple(
                str(item) for item in (usage.get("prompt_hashes") or ())
            ),
            evidence={
                "trace_id": result.trace_id,
                "rounds": int(result.rounds),
                "need_human_review": bool(result.need_human_review),
                "guard_action": str(result.guard_action),
                "error_code": str(result.error_code),
                "workspace_writes": [
                    dict(item) for item in result.workspace_writes
                ],
                "workspace_conflicts": [
                    dict(item) for item in result.workspace_conflicts
                ],
                "workspace_merges": [
                    dict(item) for item in result.workspace_merges
                ],
                "critic": [
                    item.to_dict() for item in result.critique_history
                ],
            },
            workspace=_with_after(before, after),
        )

    @staticmethod
    def _snapshot(workspace_ref: str) -> WorkspaceSnapshot:
        return fingerprint_workspace(workspace_ref) if workspace_ref else WorkspaceSnapshot()


BenchTrialRunner = Callable[
    [ExperimentSpec, int], Awaitable[tuple[Any, Path | None]]
]


class BenchExperimentAdapter:
    """把既有 bench ``RunResult`` 映射进同一 Run Record 契约。"""

    def __init__(self, run_trial: BenchTrialRunner) -> None:
        self._run_trial = run_trial

    async def execute(self, spec: ExperimentSpec, trial_index: int) -> TrialEvidence:
        result, workspace_path = await self._run_trial(spec, trial_index)
        agent_run = result.agent_run
        usage = agent_run.usage
        call_steps = [
            step for step in agent_run.steps if getattr(step, "action", "") == "call_tool"
        ]
        patch = str(getattr(result, "patch", "") or "")
        workspace = (
            fingerprint_workspace(workspace_path)
            if workspace_path is not None
            else WorkspaceSnapshot()
        )
        return TrialEvidence(
            status=str(result.outcome),
            plan=tuple(dict(item) for item in agent_run.plan),
            node_path=tuple(
                f"step:{step.index}:{step.tool or step.action}"
                for step in agent_run.steps
            ),
            per_agent_outputs={},
            final_text=str(agent_run.stop_reason),
            metrics=RunMetrics(
                llm_calls=int(usage.llm_calls),
                prompt_tokens=int(usage.prompt_tokens),
                completion_tokens=int(usage.completion_tokens),
                cost_usd=float(usage.cost_usd),
                llm_latency_ms=float(usage.llm_latency_ms),
                tool_calls=len(call_steps),
                tool_errors=sum(
                    1 for step in call_steps if getattr(step, "outcome", "") == "failed"
                ),
                tool_arg_rejections=sum(
                    1 for step in call_steps if getattr(step, "outcome", "") == "rejected"
                ),
            ),
            prompt_hashes=tuple(getattr(usage, "prompt_hashes", ()) or ()),
            evidence={
                "task_id": result.task_id,
                "arm": result.arm,
                "outcome": result.outcome,
                "budget_tier": result.budget_tier,
                "baseline_cost_usd": result.baseline_cost_usd,
                "baseline_tokens": result.baseline_tokens,
                "agent_status": agent_run.status,
                "base_commit": result.base_commit,
                "patch_touched_tests": bool(result.patch_touched_tests),
                "fail_first_exit_code": int(result.fail_first_exit_code),
                "verdict_exit_code": int(result.verdict_exit_code),
                "verdict_output": str(result.verdict_output)[:_EVIDENCE_TEXT_LIMIT],
                "patch_excerpt": patch[:_EVIDENCE_TEXT_LIMIT],
                "patch_hash": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                "review_rounds": int(agent_run.review_rounds),
                "subtask_count": int(agent_run.subtask_count),
                "finish_reasons": list(
                    getattr(usage, "finish_reasons", ()) or ()
                ),
                "reasoning_chars": int(
                    getattr(usage, "reasoning_chars", 0) or 0
                ),
                "workspace_writes": [
                    dict(item)
                    for item in (
                        getattr(agent_run, "workspace_writes", ()) or ()
                    )
                ],
                "workspace_conflicts": [
                    dict(item)
                    for item in (
                        getattr(agent_run, "workspace_conflicts", ()) or ()
                    )
                ],
                "workspace_merges": [
                    dict(item)
                    for item in (
                        getattr(agent_run, "workspace_merges", ()) or ()
                    )
                ],
                "steps": [_step_evidence(step) for step in agent_run.steps],
            },
            workspace=workspace,
            error=str(agent_run.error),
        )


def _with_after(
    before: WorkspaceSnapshot, after: WorkspaceSnapshot
) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        path=after.path or before.path,
        before_hash=before.before_hash,
        after_hash=after.after_hash,
        files_before=before.files_before,
        files_after=after.files_after,
    )


__all__ = [
    "BenchExperimentAdapter",
    "BenchTrialRunner",
    "CollaborationExperimentAdapter",
]
