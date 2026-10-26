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
        await record_audit(
            AuditEntry(
                trace_id=run.trace_id,
                session_id=run.request.repo,
                agent_name="delivery_lifecycle",
                agent_output=current.value,
                intent="delivery_lifecycle",
                extra={
                    "run_id": run.run_id,
                    "from_state": previous.value if previous else "",
                    "to_state": current.value,
                    **detail,
                },
            )
        )
