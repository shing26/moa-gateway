from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    url: str
    head_sha: str
