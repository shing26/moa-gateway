from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.code_review_pipeline.change.bundle import ChangeBundle, FileChange
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.delivery.models import (
    SCHEMA_VERSION,
    AgentAttempt,
    AgentStep,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    DeliveryUsage,
    EvidenceRef,
    FailureCode,
    FailureInfo,
    PushOutcome,
    VerificationEvidence,
    VerifierVerdict,
)
from apps.code_review_pipeline.merge_store import MergeRecord


class DeliveryRunStore:
    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def _path(self, run_id: str) -> Path:
        safe = "".join(
            char for char in run_id if char.isalnum() or char in {"-", "_", "."}
        )
        if not safe or safe != run_id:
            raise ValueError(f"invalid run_id: {run_id!r}")
        return self._directory / f"{safe}.json"

    async def save(self, run: DeliveryRun) -> None:
        await asyncio.to_thread(self._write, run)

    def _write(self, run: DeliveryRun) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path(run.run_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(run.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def load(self, run_id: str) -> DeliveryRun:
        path = self._path(run_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _run_from_dict(payload)

    def view(self, run: DeliveryRun, merge_store: Any) -> DeliveryRun:
        if run.push is None:
            return run
        record = merge_store.get(
            (
                run.request.repo,
                run.push.pull_request.number,
                run.push.pull_request.head_sha,
            )
        )
        return run if record is None else replace(run, merge=record)


def _request_from_dict(payload: dict[str, Any]) -> DeliveryRequest:
    return DeliveryRequest(
        trace_id=str(payload["trace_id"]),
        repo=str(payload["repo"]),
        base_sha=str(payload["base_sha"]),
        base_branch=str(payload["base_branch"]),
        branch=str(payload["branch"]),
        goal=str(payload["goal"]),
        title=str(payload["title"]),
        body=str(payload.get("body", "")),
        origin=str(payload.get("origin", "manual")),
        source_ref=str(payload.get("source_ref", "")),
    )


def _attempt_from_dict(payload: dict[str, Any]) -> AgentAttempt:
    usage = dict(payload.get("usage") or {})
    return AgentAttempt(
        role=payload["role"],
        status=payload["status"],
        summary=str(payload.get("summary", "")),
        stop_reason=str(payload.get("stop_reason", "")),
        usage=DeliveryUsage(
            llm_calls=int(usage.get("llm_calls", 0) or 0),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=float(usage.get("llm_latency_ms", 0.0) or 0.0),
        ),
        steps=tuple(
            AgentStep(
                index=int(step["index"]),
                action=step["action"],
                tool=str(step.get("tool", "")),
                arguments=dict(step.get("arguments") or {}),
                observation=str(step.get("observation", "")),
                outcome=step.get("outcome", ""),
                elapsed_ms=float(step.get("elapsed_ms", 0.0) or 0.0),
            )
            for step in payload.get("steps", ())
        ),
    )


def _bundle_from_dict(payload: dict[str, Any]) -> ChangeBundle:
    return ChangeBundle(
        base_sha=str(payload["base_sha"]),
        branch=str(payload["branch"]),
        title=str(payload["title"]),
        body=str(payload.get("body", "")),
        files=tuple(
            FileChange(
                path=str(item["path"]),
                kind=str(item["kind"]),
                before_hash=str(item.get("before_hash", "")),
                after_hash=str(item.get("after_hash", "")),
                mode=str(item.get("mode", "100644")),
            )
            for item in payload.get("files", ())
        ),
        patch=str(payload.get("patch", "")),
        patch_hash=str(payload["patch_hash"]),
        verify_command=tuple(
            str(item) for item in payload.get("verify_command", ())
        ),
        verify_exit_code=int(payload["verify_exit_code"]),
        verify_output=str(payload.get("verify_output", "")),
        verify_output_hash=str(payload.get("verify_output_hash", "")),
        model=str(payload["model"]),
        trace_id=str(payload.get("trace_id", "")),
    )


def _merge_from_dict(payload: dict[str, Any]) -> MergeRecord:
    return MergeRecord(
        repo=str(payload["repo"]),
        pr_number=int(payload["pr_number"]),
        head_sha=str(payload["head_sha"]),
        state=str(payload.get("state", "watching")),
        ci_state=str(payload.get("ci_state", "")),
        ci_failing=tuple(
            dict(item) for item in payload.get("ci_failing", ())
        ),
        card_message_id=str(payload.get("card_message_id", "")),
        approver=str(payload.get("approver", "")),
        merged_sha=str(payload.get("merged_sha", "")),
        failure_reason=str(payload.get("failure_reason", "")),
        run_id=str(payload.get("run_id", "")),
        trace_id=str(payload.get("trace_id", "")),
        bundle_hash=str(payload.get("bundle_hash", "")),
        transitions=tuple(
            dict(item) for item in payload.get("transitions", ())
        ),
    )


def _run_from_dict(payload: dict[str, Any]) -> DeliveryRun:
    schema_version = int(payload["schema_version"])
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported DeliveryRun schema: {schema_version} "
            f"(expected {SCHEMA_VERSION})"
        )

    attempts = tuple(
        _attempt_from_dict(dict(item))
        for item in payload.get("attempts", ())
    )
    verdicts = tuple(
        VerifierVerdict(
            decision=item["decision"],
            reason=str(item.get("reason", "")),
            instructions=str(item.get("instructions", "")),
            evidence=tuple(
                EvidenceRef(
                    kind=evidence["kind"],
                    ref=str(evidence["ref"]),
                    observation=str(evidence["observation"]),
                )
                for evidence in item.get("evidence", ())
            ),
            reviewed_bundle_hash=str(item["reviewed_bundle_hash"]),
            reviewed_patch_hash=str(item["reviewed_patch_hash"]),
            attempt=_attempt_from_dict(dict(item["attempt"])),
        )
        for item in payload.get("verifier_verdicts", ())
    )
    verification_payload = payload.get("verification")
    verification = (
        VerificationEvidence(
            command=tuple(
                str(item) for item in verification_payload.get("command", ())
            ),
            exit_code=int(verification_payload["exit_code"]),
            output_hash=str(verification_payload["output_hash"]),
            output_excerpt=str(verification_payload.get("output_excerpt", "")),
        )
        if verification_payload
        else None
    )
    bundle_payload = payload.get("bundle")
    bundle = _bundle_from_dict(dict(bundle_payload)) if bundle_payload else None
    push_payload = payload.get("push")
    push = (
        PushOutcome(
            branch=str(push_payload["branch"]),
            commit_sha=str(push_payload["commit_sha"]),
            pull_request=PullRequestRef(
                number=int(push_payload["pull_request"]["number"]),
                url=str(push_payload["pull_request"]["url"]),
                head_sha=str(push_payload["pull_request"]["head_sha"]),
            ),
        )
        if push_payload
        else None
    )
    merge_payload = payload.get("merge")
    merge = _merge_from_dict(dict(merge_payload)) if merge_payload else None
    failure_payload = payload.get("failure")
    failure = (
        FailureInfo(
            FailureCode(failure_payload["code"]),
            str(failure_payload["message"]),
        )
        if failure_payload
        else None
    )
    usage = dict(payload["usage"])
    return DeliveryRun(
        schema_version=schema_version,
        run_id=str(payload["run_id"]),
        trace_id=str(payload["trace_id"]),
        request=_request_from_dict(dict(payload["request"])),
        topology=payload["topology"],
        state=DeliveryRunState(payload["state"]),
        attempts=attempts,
        verification=verification,
        verifier_verdicts=verdicts,
        bundle=bundle,
        push=push,
        merge=merge,
        usage=DeliveryUsage(
            llm_calls=int(usage.get("llm_calls", 0) or 0),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
            llm_latency_ms=float(usage.get("llm_latency_ms", 0.0) or 0.0),
        ),
        failure=failure,
        started_at=datetime.fromisoformat(str(payload["started_at"])),
        updated_at=datetime.fromisoformat(str(payload["updated_at"])),
    )
