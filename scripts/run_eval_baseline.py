"""Run the pre-registered eval baseline (issue #95): one governed run per case.

Each case is a governor-run delivery against the case's historical base SHA:

    python -m app.cli delivery run --repo shing26/moa-gateway \
        --base-sha <case.base_sha> --branch eval/<case-id>-<stamp> \
        --title "[eval] <case-id>" --origin manual --source-ref <case-id> \
        --goal <case.goal>

The run stops at the human push gate.  Nothing is ever pushed, merged or
opened as a PR: this process holds no TTY, and the gate denies non-TTY input
by contract.  Records land in the delivery run store and are scored later by
``eval.l0``.

Configuration overrides are process-level only — ``.env`` is never edited.
The values are the snapshot frozen in issue #95 and repeated here so a reader
can reproduce the run without reading the issue.

Usage (one case at a time is fine; the report file accumulates):

    python scripts/run_eval_baseline.py --only biz-050
    python scripts/run_eval_baseline.py            # every case, fixed order
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.schema import EvalCase, load_cases

REPO = "shing26/moa-gateway"
CLASS_ORDER = ("business", "failure", "boundary", "security")

#: Frozen in issue #95 §2 (v1.2 amendment). ``.env`` stays untouched; these are
#: process-level overrides. The model is pinned here rather than inherited: the
#: ``.env`` value 404s on this account and ``nemotron-3.5-lightning`` answers 200
#: with empty content, so the protocol registered ``poolside/laguna-xs-2.1``
#: (the only candidate that returned real text) explicitly.
ENV_OVERRIDES = {
    "CHANGE_AGENT_MODEL": "poolside/laguna-xs-2.1",
    "CHANGE_AGENT_PUSH_ENABLED": "1",
    "LLM_TIMEOUT": "300",
    "DELIVERY_MAX_STEPS": "120",
    "DELIVERY_MAX_TOKENS": "3000000",
    "DISABLE_AIOHTTP_TRANSPORT": "True",
    "NO_PROXY": "integrate.api.nvidia.com,api.github.com,codeload.github.com,github.com",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}


def build_command(case: EvalCase, stamp: str) -> list[str]:
    """The delivery-run argv for one case (pure; testable without subprocess)."""
    return [
        sys.executable,
        "-m",
        "app.cli",
        "delivery",
        "run",
        "--repo",
        REPO,
        "--base-sha",
        case.base_sha,
        "--base-branch",
        "main",
        "--branch",
        f"eval/{case.id}-{stamp}",
        "--title",
        f"[eval] {case.id}",
        "--origin",
        "manual",
        "--source-ref",
        case.id,
        "--goal",
        case.goal,
    ]


def build_env(case: EvalCase, base_env: dict[str, str]) -> dict[str, str]:
    """Process env for one case: frozen overrides plus the case's verify command.

    The verify command is ALWAYS set explicitly. A case without one gets the
    empty string, which the pipeline reads as "no verify command" and stops at
    NO_VERIFY_COMMAND - it must never fall through to ``.env``'s default, or the
    run would be gated by a command the case never declared.
    """
    env = dict(base_env)
    env.update(ENV_OVERRIDES)
    env["CHANGE_AGENT_VERIFY_COMMAND"] = case.verify_commands[0] if case.verify_commands else ""
    return env


def case_arm(case: EvalCase) -> str:
    """Which pre-registered arm a case belongs to (issue #95 §3.2).

    ``live`` - the case declares a verify command and runs the whole pipeline.
    ``gate_state`` - synthesized cases carry no verify command by design. They
    are NOT replayable: ``app/cli.py:392-395`` refuses to start a delivery run
    without ``CHANGE_AGENT_VERIFY_COMMAND`` (exit 2, no run record), and the
    corpus deliberately declares none. Injecting one would gate the run on a
    command the case never declared, so this runner records them as deferred
    instead of pretending to measure them (see ``GATE_STATE_REASON``).
    """
    return "live" if case.verify_commands else "gate_state"


#: Why gate_state cases are not run by this runner. Repeated in every row and
#: in the report so a reader cannot mistake "deferred" for "passed".
GATE_STATE_REASON = (
    "no verify command declared; the CLI refuses to start a delivery run "
    "without CHANGE_AGENT_VERIFY_COMMAND (app/cli.py:392-395, exit 2). "
    "Gate-state cases need their own harness; they are not L0-scoreable."
)


def _ordered_cases(cases_dir: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for cls in CLASS_ORDER:
        cases.extend(load_cases(str(cases_dir / f"{cls}.jsonl")))
    return cases


def _parse_field(stdout: str, name: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(f"{name}"):
            return line.split(":", 1)[1].strip()
    return ""


def _run_record_dir() -> Path:
    from app.config import settings

    return Path(settings.delivery_run_record_dir)


def _find_run_id_for_branch(branch: str) -> str:
    """Recover the run id of a timed-out child by matching its branch.

    The CLI prints ``run_id`` only at the approval gate or when the flow
    returns, so a wall-clock kill loses stdout's copy. Every attempt writes a
    record immediately, so the record store still knows it.
    """
    record_dir = _run_record_dir()
    if not record_dir.is_dir():
        return ""
    for path in sorted(record_dir.glob("dlv-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        request = record.get("request") or {}
        if request.get("branch") == branch:
            return str(record.get("run_id") or path.stem)
    return ""


def _kill_tree(pid: int) -> None:
    """Kill the child and its descendants; verify runs spawn grandchildren."""
    try:
        if os.name == "nt":
            subprocess.run(  # fixed argv, no shell
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(os.getpgid(pid), 9)
    except (OSError, ProcessLookupError, PermissionError):
        pass


def _deferred_row(case: EvalCase, branch: str) -> dict:
    """The row for a gate_state case: recorded, never run.

    A row (not silence) so the report's denominator stays 26 and a reader sees
    which nine cases have no measurement instead of reading an absent row as a
    pass.
    """
    return {
        "case_id": case.id,
        "class": case.class_,
        "arm": "gate_state",
        "base_sha": case.base_sha,
        "branch": branch,
        "run_id": "",
        "state": "not_run_by_design",
        "exit_code": None,
        "wall_timeout": False,
        "elapsed_s": 0.0,
        "log": "",
        "reason": GATE_STATE_REASON,
    }


def run_case(
    case: EvalCase,
    *,
    stamp: str,
    timeout_s: float,
    log_dir: Path,
) -> dict:
    """Run one case; return the report row (never raises on a failed run)."""
    cmd = build_command(case, stamp)
    env = build_env(case, dict(os.environ))
    branch = f"eval/{case.id}-{stamp}"
    if case_arm(case) == "gate_state":
        return _deferred_row(case, branch)

    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    started = dt.datetime.now(dt.timezone.utc)
    timed_out = False
    proc = subprocess.Popen(  # fixed argv, no shell
        cmd,
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,  # the push gate must always see a non-TTY
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        exit_code: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        # Bounded: a child that ignores the kill must not hang the baseline
        # forever. Whatever it printed is lost, the run record still has it.
        try:
            stdout, stderr = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        exit_code = None
    elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()

    stdout = stdout or ""
    stderr = stderr or ""
    run_id = _parse_field(stdout, "run_id") or (_find_run_id_for_branch(branch) if timed_out else "")

    log_dir.mkdir(parents=True, exist_ok=True)
    # Stamped: two sessions appending to one report must not overwrite each
    # other's log for the same case id.
    log_path = log_dir / f"{case.id}-{stamp}.log"
    log_path.write_text(
        f"$ {' '.join(cmd[:6])} ... --source-ref {case.id}\n\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n",
        encoding="utf-8",
    )
    try:
        log_ref = str(log_path.relative_to(ROOT))
    except ValueError:
        log_ref = str(log_path)

    return {
        "case_id": case.id,
        "class": case.class_,
        "arm": case_arm(case),
        "base_sha": case.base_sha,
        "branch": branch,
        "run_id": run_id,
        "state": _parse_field(stdout, "state"),
        "exit_code": exit_code,
        "wall_timeout": timed_out,
        "elapsed_s": round(elapsed, 1),
        "log": log_ref,
    }


def prepare_report(report_path: Path, stamp: str) -> dict:
    """Load an existing report or start one; metadata always comes from here.

    Only ``rows`` carry over from a previous session. A report written by an
    older protocol version must not keep claiming that version (or a blank
    model) after a newer session appends to it.
    """
    report: dict = {}
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report.get("rows"), list):
        report["rows"] = []
    report.update({
        "protocol": "issue #95 (v1.2)",
        "stamp": stamp,
        "model": ENV_OVERRIDES["CHANGE_AGENT_MODEL"],
        "env_overrides": dict(ENV_OVERRIDES),
        "deferred_arm": GATE_STATE_REASON,
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-dir", default=str(ROOT / "eval" / "cases"))
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--timeout-s", type=float, default=5400.0)
    parser.add_argument("--report", default="")
    parser.add_argument("--stamp", default="")
    args = parser.parse_args(argv)

    stamp = args.stamp or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # One report file for the whole baseline: the two-session plan appends to it.
    report_path = Path(args.report or (ROOT / "evals" / "reports" / "eval-baseline-v1.json"))
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    log_dir = report_path.parent / "eval-baseline-v1-logs"

    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    cases = [c for c in _ordered_cases(Path(args.cases_dir)) if not wanted or c.id in wanted]
    unknown = wanted - {c.id for c in cases}
    if unknown:
        raise SystemExit(f"unknown case ids: {sorted(unknown)}")

    report = prepare_report(report_path, stamp)

    for index, case in enumerate(cases, start=1):
        print(f"[{index}/{len(cases)}] {case.id} ({case.class_}) ...", flush=True)
        row = run_case(case, stamp=stamp, timeout_s=args.timeout_s, log_dir=log_dir)
        report["rows"] = [r for r in report["rows"] if r["case_id"] != case.id]
        report["rows"].append(row)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: an interrupted write must not leave a half report that the
        # next session cannot read back.
        tmp_path = report_path.with_suffix(report_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, report_path)
        print(
            f"    run_id={row['run_id'] or '-'} state={row['state'] or '-'} "
            f"exit={row['exit_code']} {row['elapsed_s']}s"
            + (" TIMEOUT" if row["wall_timeout"] else ""),
            flush=True,
        )

    print(f"\nreport: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
