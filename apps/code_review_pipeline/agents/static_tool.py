from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from apps.code_review_pipeline.schemas.pipeline import Finding

logger = logging.getLogger("moa.code_review.static_tool")


class RuffExecutionError(Exception):
    """Raised when ruff fails to execute or return parseable output."""


class BanditExecutionError(Exception):
    """Raised when bandit fails to execute or return parseable output."""


def _write_sandbox(files: list[dict[str, str]], dest: Path) -> None:
    """Write in-memory file contents to a sandbox directory."""
    for item in files:
        rel = item["filename"]
        content = item.get("content") or ""
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _find_ruff() -> str:
    """Return the ruff CLI executable path."""
    candidates = ["ruff", shutil.which("ruff") or ""]
    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate) or candidate
        if os.path.isfile(path) or shutil.which(path):
            return path
    raise RuffExecutionError(
        "ruff CLI not found. Install it with: pip install ruff or uv tool install ruff"
    )


def _find_bandit() -> str:
    """Return the bandit CLI executable path."""
    candidates = ["bandit", shutil.which("bandit") or ""]
    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate) or candidate
        if os.path.isfile(path) or shutil.which(path):
            return path
    raise BanditExecutionError(
        "bandit CLI not found. Install it with: pip install bandit"
    )


def run_ruff_on_files(
    files: list[dict[str, str]],
    *,
    select: str = "E,F,I,S",
    ignore: str = "",
    line_length: int = 120,
) -> list[Finding]:
    """
    Run ruff against an in-memory list of changed files.

    Each item in `files` should be:
        {"filename": "pkg/module.py", "content": "source code..."}

    Returns a list of Finding instances with ruff-native evidence.
    """
    if not files:
        return []

    ruff = _find_ruff()

    with tempfile.TemporaryDirectory(prefix="code-review-ruff-") as tmp:
        root = Path(tmp)
        _write_sandbox(files, root)

        cmd = [
            ruff,
            "check",
            str(root),
            "--select",
            select,
            "--output-format",
            "json",
            "--no-cache",
            "--line-length",
            str(line_length),
        ]
        if ignore:
            cmd.extend(["--ignore", ignore])

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuffExecutionError(f"ruff timed out after {exc.timeout}s") from exc
        except OSError as exc:
            raise RuffExecutionError(f"ruff execution failed: {exc}") from exc

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode not in (0, 1):
            # ruff returns 1 when issues are found, >1 on real errors
            logger.error("ruff failed: %s", stderr)
            raise RuffExecutionError(
                f"ruff exited with code {proc.returncode}: {stderr}"
            )

        if not stdout:
            return []

        try:
            raw_findings = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuffExecutionError(
                f"ruff output is not valid JSON: {exc}\nstdout: {stdout[:500]}"
            ) from exc

    findings: list[Finding] = []
    for item in raw_findings:
        filename = item.get("filename", "")
        try:
            rel_path = Path(filename).relative_to(root)
        except ValueError:
            rel_path = Path(filename)

        location = item.get("location") or {}
        line = location.get("row", 0)
        column = location.get("column", 0)

        code = item.get("code", "")
        message = item.get("message", "")
        fix = item.get("fix") or {}
        fix_message = fix.get("message") if isinstance(fix, dict) else None

        # Map ruff severity from rule prefix.
        rule_code = str(code).upper()
        if rule_code.startswith("S"):
            severity = "high"
        elif rule_code.startswith(("E", "F")):
            severity = "medium"
        elif rule_code.startswith("I"):
            severity = "low"
        else:
            severity = "low"

        finding = Finding(
            id=f"ruff-{rule_code}-{rel_path}-{line}-{column}",
            severity=severity,
            category="style",
            file=str(rel_path),
            line=int(line or 0),
            title=f"{rule_code}: {message}" if rule_code else message,
            description=message,
            suggestion=fix_message or "",
            confidence=0.9 if fix_message else 0.7,
            team_specific=False,
            evidence=(f"ruff:{rule_code}",),
        )
        findings.append(finding)

    logger.info("ruff produced %d findings across %d files", len(findings), len(files))
    return findings


def run_bandit_on_files(
    files: list[dict[str, str]],
    *,
    confidence: str = "medium",
) -> list[Finding]:
    """
    Run bandit against an in-memory list of changed Python files.

    Each item in `files` should be:
        {"filename": "pkg/module.py", "content": "source code..."}

    Returns a list of Finding instances with bandit-native evidence.
    """
    if not files:
        return []

    bandit = _find_bandit()

    with tempfile.TemporaryDirectory(prefix="code-review-bandit-") as tmp:
        root = Path(tmp)
        _write_sandbox(files, root)

        cmd = [
            bandit,
            "-r",
            str(root),
            "-f",
            "json",
            "--severity-level",
            confidence,
        ]

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise BanditExecutionError(f"bandit timed out after {exc.timeout}s") from exc
        except OSError as exc:
            raise BanditExecutionError(f"bandit execution failed: {exc}") from exc

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode not in (0, 1):
            # bandit returns 1 when issues are found, >1 on real errors
            logger.error("bandit failed: %s", stderr)
            raise BanditExecutionError(
                f"bandit exited with code {proc.returncode}: {stderr}"
            )

        if not stdout:
            return []

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BanditExecutionError(
                f"bandit output is not valid JSON: {exc}\nstdout: {stdout[:500]}"
            ) from exc

        # Bandit wraps results under "results".
        raw_findings = payload.get("results", []) if isinstance(payload, dict) else []

    findings: list[Finding] = []
    for item in raw_findings:
        filename = item.get("filename", "")
        try:
            rel_path = Path(filename).relative_to(root)
        except ValueError:
            rel_path = Path(filename)

        line = item.get("line_number", 0)
        col = item.get("col_offset", 0) or item.get("column", 0)
        code = item.get("test_id", item.get("code", ""))
        message = item.get("issue_text", "")
        severity = (item.get("issue_severity") or "medium").lower()

        # bandit severity values: HIGH, MEDIUM, LOW
        if severity == "high":
            mapped_severity = "high"
        elif severity == "medium":
            mapped_severity = "medium"
        else:
            mapped_severity = "low"

        finding = Finding(
            id=f"bandit-{code}-{rel_path}-{line}-{col}",
            severity=mapped_severity,
            category="security",
            file=str(rel_path),
            line=int(line or 0),
            title=f"{code}: {message}" if code else message,
            description=message,
            suggestion="",
            confidence=0.8,
            team_specific=False,
            evidence=(f"bandit:{code}",),
        )
        findings.append(finding)

    logger.info("bandit produced %d findings across %d files", len(findings), len(files))
    return findings


def run_static_tools(
    files: list[dict[str, str]],
    *,
    run_ruff: bool = True,
    run_bandit: bool = True,
) -> list[Finding]:
    """
    Run enabled static tools on the given changed files.

    Returns a unified list of Finding instances.
    """
    all_findings: list[Finding] = []

    if run_ruff:
        try:
            all_findings.extend(run_ruff_on_files(files))
        except RuffExecutionError as exc:
            logger.error("ruff tool failed: %s", exc)

    if run_bandit:
        try:
            all_findings.extend(run_bandit_on_files(files))
        except BanditExecutionError as exc:
            logger.error("bandit tool failed: %s", exc)

    # Deduplicate by tool+file+line+code to avoid noisy repeats.
    seen: set[str] = set()
    deduped: list[Finding] = []
    for finding in all_findings:
        key = f"{finding.evidence[0]}:{finding.file}:{finding.line}:{finding.id}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)

    return deduped
