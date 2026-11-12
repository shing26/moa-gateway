from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Normalise a JSON list or string into a tuple[str, ...]."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _env_pair_tuple(value: Any) -> tuple[tuple[str, str], ...]:
    """Normalise JSON [[str,str],...] into tuple[tuple[str,str],...]."""
    if value is None:
        return ()
    if isinstance(value, str):
        return ((value, value),)
    return tuple(tuple(pair) for pair in value)


VALID_CLASSES = frozenset({"business", "failure", "boundary", "security"})
VALID_ORACLE_TYPES = frozenset({"script", "gate_state", "llm_judge"})


@dataclass(frozen=True)
class EvalCase:
    #: Unique case identifier
    id: str
    #: Case class: business, failure, boundary, security
    class_: str
    #: Source of the case
    source: str
    #: Git SHA baseline
    base_sha: str
    #: Goal description
    goal: str
    #: Allowed paths
    allowed_paths: tuple[str, ...]
    #: Oracle type
    oracle_type: str
    #: Environment variable pairs
    env: tuple[tuple[str, str], ...] = ()
    #: Fixture references
    fixtures: tuple[str, ...] = ()
    #: Forbidden paths
    forbidden_paths: tuple[str, ...] = ()
    #: Verify commands
    verify_commands: tuple[str, ...] = ()
    #: Oracle checks
    oracle_checks: tuple[str, ...] = ()
    #: Expected failure to avoid
    expected_failure_to_avoid: str = ""
    #: Tags
    tags: tuple[str, ...] = ()

    def __post_init__(self):
        if self.class_ not in VALID_CLASSES:
            raise ValueError(f"Unknown class_: {self.class_}")
        if self.oracle_type not in VALID_ORACLE_TYPES:
            raise ValueError(f"Unknown oracle_type: {self.oracle_type}")


def load_cases(path: str) -> list[EvalCase]:
    """Read JSONL one JSON object per line; blank lines skipped."""
    cases: list[EvalCase] = []
    with open(path, "r") as f:
        lines = f.readlines()
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Line {lineno}: invalid JSON") from exc
        # Check required fields
        for field in ("id", "class_", "source", "base_sha", "goal", "allowed_paths", "oracle_type"):
            if field not in obj:
                raise ValueError(f"Line {lineno}: missing {field}")
        # Normalise tuple fields
        case_id: str = obj["id"]
        case_class: str = obj["class_"]
        case_source: str = obj["source"]
        case_base_sha: str = obj["base_sha"]
        case_goal: str = obj["goal"]
        case_allowed_paths: tuple[str, ...] = _str_tuple(obj.get("allowed_paths"))
        case_env: tuple[tuple[str, str], ...] = _env_pair_tuple(obj.get("env"))
        case_fixtures: tuple[str, ...] = _str_tuple(obj.get("fixtures"))
        case_forbidden_paths: tuple[str, ...] = _str_tuple(obj.get("forbidden_paths"))
        case_verify_commands: tuple[str, ...] = _str_tuple(obj.get("verify_commands"))
        case_oracle_type: str = obj["oracle_type"]
        case_oracle_checks: tuple[str, ...] = _str_tuple(obj.get("oracle_checks"))
        case_expected_failure_to_avoid: str = obj.get("expected_failure_to_avoid", "")
        case_tags: tuple[str, ...] = _str_tuple(obj.get("tags"))
        # Build the EvalCase (validated by __post_init__)
        case = EvalCase(
            id=case_id,
            class_=case_class,
            source=case_source,
            base_sha=case_base_sha,
            fixtures=case_fixtures,
            env=case_env,
            goal=case_goal,
            allowed_paths=case_allowed_paths,
            forbidden_paths=case_forbidden_paths,
            verify_commands=case_verify_commands,
            oracle_type=case_oracle_type,
            oracle_checks=case_oracle_checks,
            expected_failure_to_avoid=case_expected_failure_to_avoid,
            tags=case_tags,
        )
        cases.append(case)
    return cases
