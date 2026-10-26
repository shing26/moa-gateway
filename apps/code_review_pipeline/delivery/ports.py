from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from apps.code_review_pipeline.change.bundle import ChangeBundle
from apps.code_review_pipeline.change.pipeline import ChangeApproval
from apps.code_review_pipeline.delivery.models import (
    AgentAttempt,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRun,
    DeliveryRunState,
    PushOutcome,
    VerificationEvidence,
)


class PreparationStatus(str, Enum):
    READY = "ready"
    VERIFY_FAILED = "verify_failed"
    NO_CHANGES = "no_changes"
    INVALID = "invalid"


class BundleChanged(RuntimeError):
    """The concrete publisher detected a workspace/bundle mismatch."""


@dataclass(frozen=True)
class ChangePreparation:
    status: PreparationStatus
    bundle: ChangeBundle | None
    verification: VerificationEvidence | None
    message: str = ""


class ImplementerPort(Protocol):
    async def run(
        self,
        *,
        workspace: Path,
        goal: str,
        feedback: str,
        max_steps: int,
        max_tokens: int,
    ) -> AgentAttempt: ...


class ChangePreparerPort(Protocol):
    async def prepare(
        self,
        *,
        workspace: Path,
        request: DeliveryRequest,
        policy: DeliveryPolicy,
    ) -> ChangePreparation: ...


class PushApprovalPort(Protocol):
    async def request(
        self,
        *,
        run_id: str,
        bundle: ChangeBundle,
    ) -> ChangeApproval | None: ...


class ChangePublisher(Protocol):
    async def publish(
        self,
        *,
        workspace: Path,
        bundle: ChangeBundle,
        approval: ChangeApproval,
    ) -> PushOutcome: ...


class RunRecorderPort(Protocol):
    async def save(self, run: DeliveryRun) -> None: ...


class DeliveryAuditPort(Protocol):
    async def record(
        self,
        *,
        run: DeliveryRun,
        previous: DeliveryRunState | None,
        current: DeliveryRunState,
        detail: dict[str, object],
    ) -> None: ...
