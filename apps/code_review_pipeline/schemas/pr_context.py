from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PRFile:
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None = None
    sha: str | None = None


@dataclass(frozen=True)
class PRContext:
    repo: str
    pr_number: int
    head_sha: str
    base_sha: str
    title: str
    author: str
    html_url: str
    diff_url: str
    changed_files: list[PRFile] = field(default_factory=list)
    labels: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()

    @property
    def module_hints(self) -> list[str]:
        hints: list[str] = []
        for f in self.changed_files:
            parts = f.filename.split("/")
            if len(parts) >= 2:
                hints.append(parts[-2])
            else:
                hints.append(parts[0])
        # preserve order, dedupe
        seen: set[str] = set()
        ordered: list[str] = []
        for item in hints:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered
