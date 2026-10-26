from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.refs import PullRequestRef
from apps.code_review_pipeline.merge_store import MergeRecord

SCHEMA_VERSION = 1

DeliveryOrigin = Literal["manual", "issue", "ci_failure"]
DeliveryTopology = Literal["single", "verified"]
AgentRole = Literal["implementer", "verifier"]
AgentStatus = Literal["completed", "step_limit", "budget_exhausted", "error"]


@dataclass(frozen=True)
class DeliveryRequest:
    trace_id: str
    repo: str
    base_sha: str
    base_branch: str
    branch: str
    goal: str
    title: str
    body: str
    origin: DeliveryOrigin = "manual"
    source_ref: str = ""

    def __post_init__(self) -> None:
        owner, separator, name = self.repo.partition("/")
        if not separator or not owner.strip() or not name.strip() or "/" in name:
            raise ValueError(f"repo 必须是 owner/name 形式: {self.repo!r}")
        for name, value in (
            ("trace_id", self.trace_id),
            ("base_sha", self.base_sha),
            ("base_branch", self.base_branch),
            ("branch", self.branch),
            ("goal", self.goal),
            ("title", self.title),
        ):
            if not value.strip():
                raise ValueError(f"{name} 不能为空")
        if self.origin not in {"manual", "issue", "ci_failure"}:
            raise ValueError(f"origin 非法: {self.origin!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "repo": self.repo,
            "base_sha": self.base_sha,
            "base_branch": self.base_branch,
            "branch": self.branch,
            "goal": self.goal,
            "title": self.title,
            "body": self.body,
            "origin": self.origin,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class DeliveryPolicy:
    verify_command: tuple[str, ...]
    topology: DeliveryTopology = "single"
    max_revisions: int = 1
    max_steps: int = 40
    max_tokens: int = 400_000

    def __post_init__(self) -> None:
        if not self.verify_command:
            raise ValueError("verify_command 不能为空")
        if self.topology not in {"single", "verified"}:
            raise ValueError(f"topology 非法: {self.topology!r}")
        if self.max_revisions != 1:
            raise ValueError("max_revisions 固定为 1")
        if self.max_steps <= 0:
            raise ValueError("max_steps 必须为正整数")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens 必须为正整数")


@dataclass(frozen=True)
class AgentStep:
    index: int
    action: Literal["call_tool", "finish"]
    tool: str
    arguments: dict[str, Any]
    observation: str
    outcome: Literal["ok", "rejected", "failed", ""]
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "observation": self.observation,
            "outcome": self.outcome,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True)
class DeliveryUsage:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: DeliveryUsage) -> DeliveryUsage:
        return DeliveryUsage(
            llm_calls=self.llm_calls + other.llm_calls,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            llm_latency_ms=self.llm_latency_ms + other.llm_latency_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "llm_latency_ms": self.llm_latency_ms,
        }


@dataclass(frozen=True)
class AgentAttempt:
    role: AgentRole
    status: AgentStatus
    summary: str
    stop_reason: str
    usage: DeliveryUsage
    steps: tuple[AgentStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "status": self.status,
            "summary": self.summary,
            "stop_reason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class EvidenceRef:
    kind: Literal["file", "command", "diff"]
    ref: str
    observation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "observation": self.observation,
        }


@dataclass(frozen=True)
class VerifierVerdict:
    decision: Literal["approved", "revise"]
    reason: str
    instructions: str
    evidence: tuple[EvidenceRef, ...]
    reviewed_bundle_hash: str
    reviewed_patch_hash: str
    attempt: AgentAttempt

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "instructions": self.instructions,
            "evidence": [item.to_dict() for item in self.evidence],
            "reviewed_bundle_hash": self.reviewed_bundle_hash,
            "reviewed_patch_hash": self.reviewed_patch_hash,
            "attempt": self.attempt.to_dict(),
        }


@dataclass(frozen=True)
class PushOutcome:
    branch: str
    commit_sha: str
    pull_request: PullRequestRef

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "pull_request": {
                "number": self.pull_request.number,
                "url": self.pull_request.url,
                "head_sha": self.pull_request.head_sha,
            },
        }


@dataclass(frozen=True)
class VerificationEvidence:
    command: tuple[str, ...]
    exit_code: int
    output_hash: str
    output_excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "output_hash": self.output_hash,
            "output_excerpt": self.output_excerpt,
        }


class DeliveryRunState(str, Enum):
    PREPARING = "preparing"
    IMPLEMENTING = "implementing"
    CHECKING = "checking"
    VERIFIER_REVIEW = "verifier_review"
    REVISING = "revising"
    READY_FOR_PUSH_APPROVAL = "ready_for_push_approval"
    PUSHING = "pushing"
    WAITING_CI = "waiting_ci"
    AWAITING_MERGE_APPROVAL = "awaiting_merge_approval"
    MERGING = "merging"
    MERGED = "merged"
    REJECTED = "rejected"
    NEEDS_HUMAN = "needs_human"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


class FailureCode(str, Enum):
    NO_CHANGES = "no_changes"
    VERIFY_FAILED = "verify_failed"
    VERIFIER_INVALID = "verifier_invalid"
    VERIFIER_REVISE_EXHAUSTED = "verifier_revise_exhausted"
    BUNDLE_CHANGED = "bundle_changed"
    APPROVAL_REJECTED = "approval_rejected"
    PUSH_FAILED = "push_failed"
    CI_FAILED = "ci_failed"
    MERGE_SHA_MISMATCH = "merge_sha_mismatch"
    MERGE_NOT_MERGEABLE = "merge_not_mergeable"
    INFRA_ERROR = "infra_error"


@dataclass(frozen=True)
class FailureInfo:
    code: FailureCode
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": self.message}


def _merge_to_dict(record: MergeRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "repo": record.repo,
        "pr_number": record.pr_number,
        "head_sha": record.head_sha,
        "state": record.state,
        "ci_state": record.ci_state,
        "ci_failing": [dict(item) for item in record.ci_failing],
        "card_message_id": record.card_message_id,
        "approver": record.approver,
        "merged_sha": record.merged_sha,
        "failure_reason": record.failure_reason,
        "transitions": [dict(item) for item in record.transitions],
    }


@dataclass(frozen=True)
class DeliveryRun:
    schema_version: int
    run_id: str
    trace_id: str
    request: DeliveryRequest
    topology: DeliveryTopology
    state: DeliveryRunState
    attempts: tuple[AgentAttempt, ...]
    verification: VerificationEvidence | None
    verifier_verdicts: tuple[VerifierVerdict, ...]
    bundle: ChangeBundle | None
    push: PushOutcome | None
    merge: MergeRecord | None
    usage: DeliveryUsage
    failure: FailureInfo | None
    started_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "request": self.request.to_dict(),
            "topology": self.topology,
            "state": self.state.value,
            "attempts": [item.to_dict() for item in self.attempts],
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "verifier_verdicts": [
                item.to_dict() for item in self.verifier_verdicts
            ],
            "bundle": self.bundle.to_dict() if self.bundle else None,
            "push": self.push.to_dict() if self.push else None,
            "merge": _merge_to_dict(self.merge),
            "usage": self.usage.to_dict(),
            "failure": self.failure.to_dict() if self.failure else None,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
