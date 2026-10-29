from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from apps.code_review_pipeline.schemas.pipeline import Finding

FINDING_STATUS_CANDIDATE = "candidate"
FINDING_STATUS_PUBLISHED = "published"
FINDING_STATUS_DROPPED = "dropped"
FINDING_STATUS_SUPERSEDED = "superseded"
FINDING_STATUS_LEGACY = "legacy"
FINDING_STATUSES = frozenset(
    {
        FINDING_STATUS_CANDIDATE,
        FINDING_STATUS_PUBLISHED,
        FINDING_STATUS_DROPPED,
        FINDING_STATUS_SUPERSEDED,
        FINDING_STATUS_LEGACY,
    }
)

FINDING_DISPOSITIONS = frozenset(
    {
        "accepted_actionable",
        "accepted_non_actionable",
        "false_positive",
        "duplicate",
        "already_known",
    }
)
REASON_REQUIRED_DISPOSITIONS = frozenset(
    {"accepted_non_actionable", "false_positive"}
)
LABEL_SOURCES = frozenset({"human", "model_prelabel"})


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalized_file(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _semantic_terms(value: str) -> tuple[str, ...]:
    """Return order-insensitive terms for a deterministic semantic fingerprint.

    This is a lexical semantic identity, not an embedding. It intentionally
    ignores presentation noise such as punctuation, casing, repeated whitespace,
    and word order so that a rephrased equivalent finding reuses the same
    identity while genuinely different file/category/meaning still diverges.
    """
    terms = re.findall(r"[0-9a-zA-Z_\u4e00-\u9fff]+", value.casefold())
    return tuple(sorted(set(terms)))


def finding_fingerprint(finding: Finding) -> str:
    """Semantic identity used for dedupe; location/evidence noise is excluded.

    Source line numbers move frequently after rebases and fixes. Treating the
    line number as identity would turn the same defect into a different finding
    and inflate duplicate/already-known metrics. The precise current location
    remains in the persisted snapshot.
    """
    payload = {
        "category": _semantic_terms(finding.category),
        "description": _semantic_terms(finding.description),
        "file": _normalized_file(finding.file),
        "title": _semantic_terms(finding.title),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def finding_uid(
    trace_id: str,
    agent: str,
    fingerprint: str,
    revision_of: str | None = None,
) -> str:
    material = (
        f"{trace_id}\0{_normalized_text(agent).lower()}\0{fingerprint}"
        + (f"\0revision_of:{revision_of}" if revision_of else "")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PersistedFinding:
    finding_uid: str
    fingerprint: str
    trace_id: str
    agent: str
    raw_finding_id: str
    status: str
    revision_of: str | None
    severity: str
    category: str
    file: str
    line: int
    title: str
    description: str
    suggestion: str
    confidence: float
    team_specific: bool
    evidence: tuple[str, ...]
    source_agents: tuple[str, ...]
    critic_decision: str = ""
    critic_reason: str = ""
    evidence_verified: bool = False


@dataclass(frozen=True)
class FindingFeedback:
    event_id: str
    trace_id: str
    finding_uid: str
    disposition: str
    reason_code: str
    actor: str
    label_source: str = "human"
    reason_note: str = ""
    created_at: str = ""
    review_run_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id is required")
        if not self.trace_id.strip():
            raise ValueError("trace_id is required")
        if not self.finding_uid.strip():
            raise ValueError("finding_uid is required")
        if self.disposition not in FINDING_DISPOSITIONS:
            raise ValueError(f"unsupported finding disposition: {self.disposition}")
        if self.label_source not in LABEL_SOURCES:
            raise ValueError(f"unsupported label_source: {self.label_source}")
        if self.disposition in REASON_REQUIRED_DISPOSITIONS and not self.reason_code.strip():
            raise ValueError(f"reason_code is required for {self.disposition}")


@dataclass(frozen=True)
class FeedbackStatus:
    trace_id: str
    total_findings: int
    labelled_findings: int
    missing_finding_uids: tuple[str, ...]
    latest_human_by_finding: dict[str, FindingFeedback]

    @property
    def complete(self) -> bool:
        return self.total_findings == self.labelled_findings


def persisted_finding_from_result(
    *,
    trace_id: str,
    agent: str,
    finding: Finding,
    status: str = FINDING_STATUS_PUBLISHED,
    revision_of: str | None = None,
    critic_decision: str = "",
    critic_reason: str = "",
    evidence_verified: bool = False,
) -> PersistedFinding:
    if status not in FINDING_STATUSES:
        raise ValueError(f"unsupported finding status: {status}")
    revision_of = revision_of or finding.revision_of
    fingerprint = finding_fingerprint(finding)
    uid = finding_uid(trace_id, agent, fingerprint, revision_of)
    return PersistedFinding(
        finding_uid=uid,
        fingerprint=fingerprint,
        trace_id=trace_id,
        agent=agent,
        raw_finding_id=finding.id,
        status=status,
        revision_of=revision_of,
        severity=finding.severity,
        category=finding.category,
        file=_normalized_file(finding.file),
        line=int(finding.line),
        title=finding.title,
        description=finding.description,
        suggestion=finding.suggestion,
        confidence=float(finding.confidence),
        team_specific=bool(finding.team_specific),
        evidence=tuple(finding.evidence),
        source_agents=tuple(sorted({_normalized_text(item).lower() for item in finding.source_agents if item})),
        critic_decision=critic_decision,
        critic_reason=critic_reason,
        evidence_verified=evidence_verified,
    )
