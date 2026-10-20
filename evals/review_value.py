"""Review value-loop metrics for R0, Review L1, and one predeclared improvement.

The evaluator is deliberately storage-agnostic: the production worker writes
findings and feedback to PostgreSQL, while experiments can export the same
facts as JSON. This module never treats model prelabels as human evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
PR_CORPUS_PATH = ROOT / "evals" / "datasets" / "review_pr_corpus.json"
CONTROLLED_CASES_PATH = ROOT / "evals" / "datasets" / "review_controlled_cases.json"

ARMS = ("R0", "R1", "R1+I")
HUMAN_DISPOSITIONS = frozenset(
    {
        "accepted_actionable",
        "accepted_non_actionable",
        "false_positive",
        "duplicate",
        "already_known",
    }
)
SIZE_STRATA = ("small", "medium", "large")


@dataclass(frozen=True)
class ReviewEvaluationCase:
    case_id: str
    source: str
    task_file: str
    known_defect_count: int = 1
    input_redaction: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source not in {"E1", "E3"}:
            raise ValueError(f"unsupported controlled-case source: {self.source}")
        if self.known_defect_count != 1:
            raise ValueError("controlled recall cases must contain exactly one known defect")
        if "test_paths" not in self.input_redaction:
            raise ValueError("test_paths must be redacted from controlled review input")


@dataclass(frozen=True)
class ReviewRunRecord:
    case_id: str
    arm: str
    findings: tuple[dict[str, Any], ...]
    known_defects_found: tuple[str, ...] = ()
    known_defects_total: int = 0
    cost_usd: float = 0.0
    critic_cost_usd: float = 0.0
    latency_ms: float = 0.0
    model_id: str = ""
    temperature: float = 0.0
    token_budget: int = 0
    workspace_budget_seconds: int = 0
    base_sha: str = ""
    head_sha: str = ""
    improvement_code: str = ""
    changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ValueError(f"unsupported arm: {self.arm}")
        if self.known_defects_total < 0:
            raise ValueError("known_defects_total cannot be negative")
        for finding in self.findings:
            disposition = str(finding.get("disposition") or "")
            label_source = str(finding.get("label_source") or "")
            if disposition and disposition not in HUMAN_DISPOSITIONS:
                raise ValueError(f"unsupported finding disposition: {disposition}")
            if (
                disposition in {"accepted_non_actionable", "false_positive"}
                and not str(finding.get("reason_code") or "").strip()
            ):
                raise ValueError(f"reason_code is required for {disposition}")
            if disposition and label_source not in {"human", "model_prelabel"}:
                raise ValueError("labelled findings require label_source")
            if not disposition and label_source not in {"", "model_prelabel"}:
                raise ValueError(f"unsupported finding label_source: {label_source}")
            prelabel = finding.get("model_prelabel")
            if prelabel is not None:
                if not isinstance(prelabel, dict):
                    raise TypeError("model_prelabel must be an object")
                prelabel_disposition = str(prelabel.get("disposition") or "")
                if prelabel_disposition not in HUMAN_DISPOSITIONS:
                    raise ValueError(
                        f"unsupported model_prelabel disposition: {prelabel_disposition}"
                    )
                if (
                    prelabel_disposition
                    in {"accepted_non_actionable", "false_positive"}
                    and not str(prelabel.get("reason_code") or "").strip()
                ):
                    raise ValueError(
                        f"model_prelabel reason_code is required for {prelabel_disposition}"
                    )

    @property
    def known_defect_count(self) -> int:
        return int(self.known_defects_total)


def load_pr_corpus(path: Path = PR_CORPUS_PATH) -> tuple[dict[str, Any], ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = tuple(payload.get("prs", ()))
    if len(rows) != 24:
        raise ValueError(f"review corpus must contain 24 PRs, got {len(rows)}")

    by_repo: dict[str, list[Mapping[str, Any]]] = {}
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for row in rows:
        pr_id = str(row.get("pr_id") or "")
        repo = str(row.get("repo") or "")
        if not pr_id or pr_id in seen_ids:
            raise ValueError(f"duplicate or empty pr_id: {pr_id!r}")
        if not repo:
            raise ValueError(f"{pr_id}: repo is required")
        seen_ids.add(pr_id)
        url = str(row.get("url") or "")
        if not url or url in seen_urls:
            raise ValueError(f"{pr_id}: duplicate or empty URL")
        seen_urls.add(url)
        for field in ("base_sha", "head_sha"):
            value = str(row.get(field) or "")
            if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value.lower()):
                raise ValueError(f"{pr_id}: {field} is not a locked 40-hex SHA")
        additions = int(row.get("additions", -1))
        deletions = int(row.get("deletions", -1))
        diff_lines = int(row.get("diff_lines", -1))
        if min(additions, deletions) < 0 or diff_lines != additions + deletions:
            raise ValueError(f"{pr_id}: diff_lines must equal additions + deletions")
        stratum = str(row.get("size_stratum") or "")
        if stratum not in SIZE_STRATA:
            raise ValueError(f"{pr_id}: unsupported size stratum {stratum!r}")
        expected = (
            "small"
            if diff_lines <= 20
            else "medium"
            if diff_lines <= 250
            else "large"
        )
        if stratum != expected:
            raise ValueError(f"{pr_id}: size stratum {stratum} does not match {diff_lines} lines")
        by_repo.setdefault(repo, []).append(row)

    expected_repos = {
        "fastapi/fastapi",
        "pydantic/pydantic",
        "sqlalchemy/sqlalchemy",
        "redis/redis-py",
    }
    if set(by_repo) != expected_repos:
        raise ValueError(f"review corpus repositories drifted: {sorted(by_repo)}")
    for repo, repo_rows in by_repo.items():
        if len(repo_rows) != 6:
            raise ValueError(f"{repo}: expected six PRs, got {len(repo_rows)}")
        counts = {
            stratum: sum(str(row["size_stratum"]) == stratum for row in repo_rows)
            for stratum in SIZE_STRATA
        }
        if any(count != 2 for count in counts.values()):
            raise ValueError(f"{repo}: expected two PRs per size stratum, got {counts}")
    return rows


def load_controlled_cases(path: Path = CONTROLLED_CASES_PATH) -> tuple[ReviewEvaluationCase, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = tuple(
        ReviewEvaluationCase(
            case_id=str(row.get("case_id") or ""),
            source=str(row.get("source") or ""),
            task_file=str(row.get("task_file") or ""),
            known_defect_count=int(row.get("known_defect_count", 0)),
            input_redaction=tuple(str(item) for item in row.get("input_redaction", ())),
        )
        for row in payload.get("cases", ())
    )
    if len(cases) != 12:
        raise ValueError(f"controlled recall set must contain 12 cases, got {len(cases)}")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("controlled recall case_id values must be unique")
    source_counts = {
        source: sum(case.source == source for case in cases)
        for source in ("E1", "E3")
    }
    if source_counts != {"E1": 5, "E3": 7}:
        raise ValueError(f"controlled recall source mix drifted: {source_counts}")
    for case in cases:
        task_path = ROOT / case.task_file
        if not task_path.is_file():
            raise ValueError(f"{case.case_id}: missing task file {case.task_file}")
        for suffix in (".test.patch", ".solution.patch"):
            patch_path = task_path.with_name(f"{task_path.stem.removesuffix('.task')}{suffix}")
            if not patch_path.is_file():
                raise ValueError(
                    f"{case.case_id}: missing controlled-case patch {patch_path.name}"
                )
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if str(task.get("task_id") or "") != case.case_id:
            raise ValueError(f"{case.case_id}: task_id does not match case_id")
        if str(task.get("source") or "") != case.source:
            raise ValueError(f"{case.case_id}: source does not match manifest")
        base_commit = str(task.get("base_commit") or "")
        if len(base_commit) != 40 or any(
            ch not in "0123456789abcdef" for ch in base_commit.lower()
        ):
            raise ValueError(f"{case.case_id}: base_commit is not a locked 40-hex SHA")
        required_redaction = {"test_paths", "solution_patch", "task_note"}
        if not required_redaction.issubset(set(case.input_redaction)):
            raise ValueError(f"{case.case_id}: controlled input redaction is incomplete")
    return cases


def load_expected_case_shas(
    pr_path: Path = PR_CORPUS_PATH,
    controlled_path: Path = CONTROLLED_CASES_PATH,
) -> dict[str, tuple[str, str]]:
    """Load the immutable SHA pairs from the two evaluation manifests."""
    expected = {
        str(row["pr_id"]): (str(row["base_sha"]), str(row["head_sha"]))
        for row in load_pr_corpus(pr_path)
    }
    for case in load_controlled_cases(controlled_path):
        task_path = ROOT / case.task_file
        task = json.loads(task_path.read_text(encoding="utf-8"))
        base_sha = str(task["base_commit"])
        stem = task_path.stem.removesuffix(".task")
        test_patch = task_path.with_name(f"{stem}.test.patch").read_bytes()
        solution_patch = task_path.with_name(f"{stem}.solution.patch").read_bytes()
        digest = hashlib.sha1(usedforsecurity=False)
        digest.update(base_sha.encode("ascii"))
        digest.update(b"\0test\0")
        digest.update(test_patch)
        digest.update(b"\0solution\0")
        digest.update(solution_patch)
        expected[case.case_id] = (base_sha, digest.hexdigest())
    return expected


def controlled_review_input(case: ReviewEvaluationCase) -> dict[str, Any]:
    """Build the only review-visible payload for a controlled case.

    The task note, known defect, verify command, and every test-file diff are
    intentionally absent. A caller should use this helper instead of handing
    the raw task/patch files to an agent.
    """
    task_path = ROOT / case.task_file
    task = json.loads(task_path.read_text(encoding="utf-8"))
    patch_path = task_path.with_name(
        f"{task_path.stem.removesuffix('.task')}.test.patch"
    )
    hidden_paths = {str(item) for item in task.get("test_paths", ())}
    chunks: list[str] = []
    for chunk in re.split(r"(?m)(?=^diff --git )", patch_path.read_text(encoding="utf-8")):
        if not chunk.startswith("diff --git "):
            continue
        first_line = chunk.splitlines()[0]
        _, _, after = first_line.partition(" b/")
        path = after.strip()
        if path in hidden_paths or path.startswith("tests/"):
            continue
        chunks.append(chunk)
    return {
        "case_id": case.case_id,
        "source": case.source,
        "base_commit": str(task["base_commit"]),
        "head_sha": load_expected_case_shas()[case.case_id][1],
        "diff": "".join(chunks),
    }


def evaluate_runs(
    runs: Iterable[ReviewRunRecord],
    *,
    controlled_case_ids: Iterable[str] = (),
    required_case_ids: Iterable[str] = (),
    expected_case_shas: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Aggregate metrics without changing denominators or selecting samples."""
    rows = tuple(runs)
    controlled = set(controlled_case_ids)
    required = set(required_case_ids)
    if required:
        for arm in ARMS:
            arm_ids = [row.case_id for row in rows if row.arm == arm]
            duplicates = sorted({case_id for case_id in arm_ids if arm_ids.count(case_id) > 1})
            missing = sorted(required - set(arm_ids))
            extra = sorted(set(arm_ids) - required)
            if duplicates or missing or extra:
                raise ValueError(
                    f"{arm} case set drifted: missing={missing}, duplicate={duplicates}, extra={extra}"
                )
    if not controlled:
        controlled = {row.case_id for row in rows if row.known_defect_count > 0}
    for row in rows:
        if row.case_id in controlled and row.known_defect_count != 1:
            raise ValueError(
                f"{row.case_id}: controlled cases must declare exactly one known defect"
            )
    arms: dict[str, Any] = {}
    for arm in ARMS:
        selected = tuple(row for row in rows if row.arm == arm)
        if not selected:
            continue
        arms[arm] = _arm_metrics(selected, controlled)
    if set(arms) != set(ARMS):
        missing = sorted(set(ARMS) - set(arms))
        raise ValueError(f"evaluation must include R0, R1, and R1+I; missing {missing}")
    improvement = recommend_single_improvement(arms)
    protocol = _validate_fixed_protocol(
        rows,
        expected_case_shas=expected_case_shas,
        selected_improvement=improvement["code"],
    )
    return {
        "schema_version": 1,
        "protocol": protocol,
        "arms": arms,
        "comparison": _comparison(arms),
        "selected_improvement": improvement,
        "limitations": [
            "single human final reviewer; no inter-rater agreement claimed",
            "24 historical public PRs are not evidence of recurring users",
            "model prelabels are recorded separately and never count as human labels",
        ],
    }


def _validate_fixed_protocol(
    rows: tuple[ReviewRunRecord, ...],
    *,
    expected_case_shas: Mapping[str, tuple[str, str]] | None,
    selected_improvement: str,
) -> dict[str, Any]:
    """Reject reports whose arms were not produced under one locked budget."""
    first = rows[0]
    if not first.model_id.strip():
        raise ValueError("evaluation model_id is required")
    if float(first.temperature) != 0.0:
        raise ValueError("evaluation temperature must be 0")
    if int(first.token_budget) <= 0 or int(first.workspace_budget_seconds) <= 0:
        raise ValueError("evaluation token/workspace budgets must be positive")

    fields = (
        "model_id",
        "temperature",
        "token_budget",
        "workspace_budget_seconds",
    )
    for row in rows[1:]:
        for field in fields:
            if getattr(row, field) != getattr(first, field):
                raise ValueError(
                    f"evaluation protocol drifted for {row.arm}/{row.case_id}: {field}"
                )

    case_shas: dict[str, tuple[str, str]] = {}
    for row in rows:
        for field in ("base_sha", "head_sha"):
            value = str(getattr(row, field))
            if len(value) != 40 or any(
                ch not in "0123456789abcdef" for ch in value.lower()
            ):
                raise ValueError(f"evaluation {field} must be a locked 40-hex SHA")
        pair = (row.base_sha, row.head_sha)
        prior = case_shas.setdefault(row.case_id, pair)
        if prior != pair:
            raise ValueError(
                f"evaluation SHA drifted for case {row.case_id}: {prior} != {pair}"
            )
        if expected_case_shas is not None:
            expected = expected_case_shas.get(row.case_id)
            if expected is None:
                raise ValueError(f"evaluation case {row.case_id} is absent from the manifest")
            if pair != expected:
                raise ValueError(
                    f"evaluation SHA does not match manifest for {row.case_id}: "
                    f"{pair} != {expected}"
                )

    changed_paths: set[tuple[str, ...]] = set()
    for row in rows:
        paths = tuple(path.strip() for path in row.changed_paths if path.strip())
        if row.arm == "R1+I":
            if not row.improvement_code:
                raise ValueError(f"R1+I provenance missing for case {row.case_id}")
            if row.improvement_code != selected_improvement:
                raise ValueError(
                    f"R1+I improvement {row.improvement_code!r} does not match "
                    f"predeclared selection {selected_improvement!r}"
                )
            if len(paths) != 1:
                raise ValueError(
                    f"R1+I case {row.case_id} must declare exactly one changed path"
                )
            changed_paths.add(paths)
        elif row.improvement_code or paths:
            raise ValueError(f"{row.arm} case {row.case_id} must not declare an improvement")
    if len(changed_paths) > 1:
        raise ValueError(f"R1+I changed paths drifted across cases: {sorted(changed_paths)}")
    return {
        "model_id": first.model_id,
        "temperature": float(first.temperature),
        "token_budget": int(first.token_budget),
        "workspace_budget_seconds": int(first.workspace_budget_seconds),
        "locked_case_shas": len(case_shas),
        "selected_improvement": selected_improvement,
        "r1_plus_i_changed_paths": sorted(next(iter(changed_paths), ())),
    }


def _arm_metrics(
    rows: tuple[ReviewRunRecord, ...],
    controlled: set[str],
) -> dict[str, Any]:
    all_findings = [
        finding
        for row in rows
        for finding in row.findings
    ]
    findings = [
        finding
        for finding in all_findings
        if _finding_is_published(finding)
    ]
    labelled = [
        item
        for item in findings
        if item.get("disposition") in HUMAN_DISPOSITIONS
        and str(item.get("label_source") or "") == "human"
    ]
    accepted_actionable = sum(
        item.get("disposition") == "accepted_actionable" for item in labelled
    )
    accepted_non_actionable = sum(
        item.get("disposition") == "accepted_non_actionable" for item in labelled
    )
    false_positive = sum(item.get("disposition") == "false_positive" for item in labelled)
    duplicate = sum(item.get("disposition") == "duplicate" for item in labelled)
    already_known = sum(item.get("disposition") == "already_known" for item in labelled)
    factual = accepted_actionable + accepted_non_actionable
    controlled_rows = [row for row in rows if row.case_id in controlled]
    found = sum(len(set(row.known_defects_found)) for row in controlled_rows)
    total = sum(row.known_defect_count for row in controlled_rows)
    evidence = sum(item.get("evidence_verified") is True for item in findings)
    revised_uids = {
        str(item.get("finding_uid") or "")
        for item in findings
        if bool(item.get("revision_of"))
    }
    dropped_uids = {
        str(item.get("finding_uid") or "")
        for item in all_findings
        if str(item.get("status") or "") == "dropped"
        and item.get("critic_decision") == "drop"
    }
    candidate_uids = {
        str(item.get("finding_uid") or "")
        for item in all_findings
        if str(item.get("status") or "") == "candidate"
        and not bool(item.get("revision_of"))
    }
    if not candidate_uids:
        candidate_uids = {
            str(item.get("finding_uid") or "") for item in findings
        }
    candidate_count = max(len(candidate_uids - {""}), 1)
    total_cost = sum(row.cost_usd for row in rows)
    critic_cost = sum(row.critic_cost_usd for row in rows)
    return {
        "prs": len(rows),
        "findings": len(findings),
        "labelled_findings": len(labelled),
        "label_completion": _ratio(len(labelled), len(findings) or 1),
        "factual_precision": _ratio(factual, len(labelled)),
        "actionable_acceptance": _ratio(accepted_actionable, len(labelled)),
        "accepted_non_actionable_rate": _ratio(accepted_non_actionable, len(labelled)),
        "false_positive_rate": _ratio(false_positive, len(labelled)),
        "duplicate_rate": _ratio(duplicate, len(labelled)),
        "already_known_rate": _ratio(already_known, len(labelled)),
        "duplicate_or_already_known_rate": _ratio(
            duplicate + already_known, len(labelled)
        ),
        "controlled_recall": _ratio(found, total),
        "evidence_coverage": _ratio(evidence, len(findings)),
        "cost_per_pr_usd": _ratio(total_cost, len(rows)),
        "latency_per_pr_ms": _ratio(sum(row.latency_ms for row in rows), len(rows)),
        "critic_overhead_usd": critic_cost,
        "critic_overhead_per_pr_usd": _ratio(critic_cost, len(rows)),
        "revision_rate": _ratio(len(revised_uids - {""}), candidate_count),
        "critic_drop_rate": _ratio(len(dropped_uids - {""}), candidate_count),
    }


def _finding_is_published(finding: Mapping[str, Any]) -> bool:
    if "published" in finding:
        return bool(finding.get("published"))
    status = str(finding.get("status") or "")
    return not status or status == "published"


def recommend_single_improvement(arms: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Pick exactly one improvement using the approved first-match order."""
    r1 = arms["R1"]
    if float(r1["controlled_recall"]) < 0.5:
        return {
            "code": "recall_rule_or_rag",
            "change": "Add one missed-defect rule or RAG exemplar category.",
        }
    if float(r1["factual_precision"]) < 0.7:
        return {
            "code": "evidence_gate",
            "change": "Tighten the deterministic evidence gate for published findings.",
        }
    if float(r1["duplicate_or_already_known_rate"]) >= 0.25:
        return {
            "code": "fingerprint_dedup",
            "change": "Strengthen fingerprint dedupe and historical-known retrieval.",
        }
    if float(r1["accepted_non_actionable_rate"]) >= 0.25:
        return {
            "code": "actionability_rubric",
            "change": "Tighten the actionability rubric in the report prompt.",
        }
    return {
        "code": "evidence_specificity",
        "change": "Require more specific evidence citations in published findings.",
    }


def _comparison(arms: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "R0_to_R1": _delta(arms["R0"], arms["R1"]),
        "R1_to_R1+I": _delta(arms["R1"], arms["R1+I"]),
        "R0_to_R1+I": _delta(arms["R0"], arms["R1+I"]),
    }


def _delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, float]:
    fields = (
        "factual_precision",
        "actionable_acceptance",
        "controlled_recall",
        "evidence_coverage",
        "cost_per_pr_usd",
        "latency_per_pr_ms",
        "critic_overhead_per_pr_usd",
    )
    return {
        field: round(float(after.get(field, 0.0)) - float(before.get(field, 0.0)), 6)
        for field in fields
    }


def _ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def report_digest(report: Mapping[str, Any]) -> str:
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# PR Review Value Loop",
        "",
        "| arm | precision | actionable | recall | evidence | cost/PR | latency/PR |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm in ARMS:
        row = report["arms"][arm]
        lines.append(
            f"| {arm} | {row['factual_precision']:.3f} | "
            f"{row['actionable_acceptance']:.3f} | {row['controlled_recall']:.3f} | "
            f"{row['evidence_coverage']:.3f} | {row['cost_per_pr_usd']:.4f} | "
            f"{row['latency_per_pr_ms']:.0f}ms |"
        )
    improvement = report["selected_improvement"]
    lines += [
        "",
        f"Predeclared single improvement: **{improvement['code']}**",
        improvement["change"],
        "",
        "Limitations: one human final reviewer; 24 historical PRs are not recurring-user evidence.",
        f"Report digest: `{report_digest(report)}`",
    ]
    return "\n".join(lines) + "\n"
