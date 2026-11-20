from __future__ import annotations

from app.audit.models import AuditEntry
from app.audit.recorder import record as record_audit
from apps.code_review_pipeline.delivery.models import (
    DeliveryRun,
    DeliveryRunState,
)


class WalDeliveryAudit:
    async def record(
        self,
        *,
        run: DeliveryRun,
        previous: DeliveryRunState | None,
        current: DeliveryRunState,
        detail: dict[str, object],
    ) -> None:
        extra: dict[str, object] = {
            **detail,
            "run_id": run.run_id,
            "from_state": previous.value if previous else "",
            "to_state": current.value,
        }
        if run.bundle is not None:
            # 审批行（approved 等）必须能直接对到被批准的 bundle（契约 §6）。
            extra["bundle_hash"] = run.bundle.bundle_hash
        await record_audit(
            AuditEntry(
                trace_id=run.trace_id,
                session_id=run.request.repo,
                agent_name="delivery_lifecycle",
                agent_output=current.value,
                intent="delivery_lifecycle",
                extra=extra,
            )
        )
