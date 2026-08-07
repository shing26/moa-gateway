from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    category: str
    file: str
    line: int
    title: str
    description: str
    suggestion: str
    confidence: float = 0.0
    team_specific: bool = False
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentFindingResult:
    agent: str
    trace_id: str
    findings: tuple[Finding, ...]
    summary: str
    recommendation: str
    need_human_review: bool = False


@dataclass(frozen=True)
class PipelineResult:
    trace_id: str
    pr: PRContext
    triage: AgentFindingResult
    static_analysis: AgentFindingResult
    semantic_review: AgentFindingResult
    test_coverage: AgentFindingResult
    report: AgentFindingResult
    overall_need_human_review: bool = False
