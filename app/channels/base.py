from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelMessage:
    channel: str
    target: str
    text: str
    trace_id: str
    need_human_review: bool = False


class ChannelAdapter(ABC):
    @abstractmethod
    async def send(self, message: ChannelMessage) -> bool:
        raise NotImplementedError()
