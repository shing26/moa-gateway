from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutboundResponse:
    text: str
    channel: str
    channel_target: str
    need_human_review: bool = False
    audit_ref: str | None = None


class ResponseAdapter:
    def adapt(self, raw: str, *, channel: str, target: str) -> OutboundResponse:
        if len(raw) > 4000:
            raw = raw[:3997] + "..."
        return OutboundResponse(text=raw, channel=channel, channel_target=target)
