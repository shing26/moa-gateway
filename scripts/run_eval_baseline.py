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
    python scripts/run_eval_baseline.py --rounds 3 # protocol multiround-v1

Each row carries ``round_index``; the merge is cumulative — an existing
``(case_id, round_index)`` is never overwritten, a new round is appended.
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

from eval import gate_state, l0
from eval.schema import EvalCase, load_cases

REPO = "shing26/moa-gateway"
CLASS_ORDER = ("business", "failure", "boundary", "security")

#: Written into every report. ``--rounds 1`` keeps the old single-run shape plus
#: ``round_index: 0`` per row; more rounds append instead of overwriting.
PROTOCOL_VERSION = "multiround-v1"

#: Frozen in issue #95 §2 (v1.4 amendment). ``.env`` stays untouched; these are
#: process-level overrides. The model is pinned here rather than inherited: the
#: ``.env`` value 404s on this account. v1.2 had registered ``poolside/laguna-xs-2.1``
#: on a connectivity probe alone; the first governed smoke run then showed it
#: cannot drive the tool loop at all - 121 steps, every one a ``read_file``, zero
#: writes, 1.37M tokens, ``budget_exhausted``. v1.4 pins
#: ``nvidia/nemotron-3.5-super-vl-120b-a12b``, the model behind the ledger's
#: 28/28 v19 run, after probing that it emits ``write_file`` tool calls in seconds.
ENV_OVERRIDES = {
    "CHANGE_AGENT_MODEL": "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "CHANGE_AGENT_PUSH_ENABLED": "1",
    # 评测进程没有 TTY，推送闸门应停在终端端口并立即拒绝（终态 rejected），
    # 由 L0 打分。若继承 .env 的 FEISHU_PUSH_APPROVAL_ENABLED=1，每条越过
    # 推送门的用例都会发一张真飞书卡并阻塞 FEISHU_PUSH_APPROVAL_TIMEOUT_S
    # （本机 1800s），既污染会话，也把单条墙钟从分钟级拖到半小时级，终态还会
    # 变成 needs_human/approval_timed_out，与 v2 基线不可比。进程级钉死为 0。
    "FEISHU_PUSH_APPROVAL_ENABLED": "0",
    # 臂必须是「同一个模型」。`app/cli.py` 用 `LLMConfig.from_env("LLM")` 组配置、
    # 只覆盖 `model`，所以真正生效的降级链是 `LLM_FALLBACK_MODELS`
    # （`CHANGE_AGENT_FALLBACK_MODELS` 没有任何读取方）。不钉空的话，主模型 429
    # 会静默换到 minimax / lightning 继续干活，报告里的 `model` 就不是实际执行的
    # 模型。实测一轮 biz-050：92 次 429，minimax 被拉起 29 次。
    "LLM_FALLBACK_MODELS": "",
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

    The verify command is ALWAYS set explicitly. Probe cases never reach this
    point (they run in-process); if one ever did, the empty string makes the
    CLI refuse with exit 2 instead of inheriting ``.env``'s default. The four
    live gate-state cases declare a neutral command in the corpus itself, so the
    runner still injects nothing the case does not declare.
    """
    env = dict(base_env)
    env.update(ENV_OVERRIDES)
    env["CHANGE_AGENT_VERIFY_COMMAND"] = case.verify_commands[0] if case.verify_commands else ""
    return env


def case_arm(case: EvalCase) -> str:
    """Which arm a case belongs to.

    ``gate_state`` - the case carries the ``gate-state-probe`` tag: its gate is
    measured by constructing it directly (``eval/gate_state.py``), with no model,
    no subprocess and no network. These are the cases a ``delivery run`` cannot
    measure: three declare ``(no LLM)`` and two gate mechanisms are unreachable
    from a normal model run.

    ``live`` - everything else. Four gate-state cases are live too: the corpus
    declares their neutral verify command, the run executes, and the run record
    is scored against ``oracle_checks`` afterwards (``evaluate_record``).
    """
    return "gate_state" if gate_state.is_probe_case(case) else "live"


#: How the probe arm works. Repeated in the report so a reader cannot mistake
#: an in-process gate measurement for a model run.
GATE_STATE_ARM = (
    "deterministic probe in eval/gate_state.py: the gate is constructed "
    "directly (no model, no subprocess, no network); judged by oracle_checks."
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

    directory = Path(settings.delivery_run_record_dir)
    if not directory.is_absolute():
        # The child runs with cwd=ROOT; resolve the same way here so a runner
        # started from another directory still reads the child's records.
        directory = ROOT / directory
    return directory


def l0_payload(report: l0.L0Report) -> dict:
    """Serialise an ``eval.l0`` report without changing its criteria.

    The six checks, their pass flags and their details land in the row so the
    report file alone justifies every verdict.
    """
    return {
        "contract_passed": report.contract_passed,
        "all_passed": report.all_passed,
        "checks": {check.name: check.passed for check in report.checks},
        "details": {check.name: check.detail for check in report.checks},
        "shape": {name: check.detail for name, check in report.shape().items()},
    }


def attach_l0(row: dict, case: EvalCase, record: dict | None) -> dict:
    """Attach the existing l0 verdict to a live row (pure; caller does the IO).

    A missing record becomes ``l0: None`` plus a reason: the row must never be
    dressed up as a pass or a fail when no judgement could be made.
    """
    if record is None:
        row["l0"] = None
        row["l0_reason"] = f"no readable run record for run_id={row.get('run_id') or '-'}"
    else:
        row["l0"] = l0_payload(l0.check_run(record, case))
    return row


def _load_run_record(run_id: str) -> dict | None:
    if not run_id:
        return None
    path = _run_record_dir() / f"{run_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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


def row_key(row: dict) -> tuple[str, int]:
    """The cumulative identity of a report row.

    ``round_index`` is optional so a report written before multiround support
    is read as round 0 rather than being silently re-run and overwritten.
    """
    return (str(row.get("case_id") or ""), int(row.get("round_index", 0) or 0))


def merge_rows(rows: list[dict], incoming: list[dict]) -> list[dict]:
    """Append ``incoming`` rows, never overwriting an existing key (pure)."""
    merged = list(rows)
    seen = {row_key(row) for row in merged}
    for row in incoming:
        key = row_key(row)
        if key in seen:
            continue
        merged.append(row)
        seen.add(key)
    return merged


def _gate_state_row(case: EvalCase, branch: str, round_index: int = 0) -> dict:
    """Run one in-process probe and return its verdict row.

    No child process and no log: the verdict carries every check's detail, and
    the probe arm is the only arm that can be reproduced without model, network
    or Postgres.
    """
    started = dt.datetime.now(dt.timezone.utc)
    try:
        verdict = gate_state.run_probe(case)
    except Exception as exc:
        # One broken probe must not stop the baseline: its row says so explicitly.
        verdict = gate_state.failed_verdict(
            case, f"probe failed: {type(exc).__name__}: {exc}"
        )
    elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
    return {
        "case_id": case.id,
        "class": case.class_,
        "arm": "gate_state",
        "base_sha": case.base_sha,
        "branch": branch,
        "run_id": "",
        "state": verdict.state,
        "exit_code": None,
        "wall_timeout": False,
        "elapsed_s": round(elapsed, 1),
        "log": "",
        "reason": GATE_STATE_ARM,
        "gate_state": verdict.to_dict(),
        "round_index": round_index,
    }


def run_case(
    case: EvalCase,
    *,
    stamp: str,
    timeout_s: float,
    log_dir: Path,
    round_index: int = 0,
) -> dict:
    """Run one case; return the report row (never raises on a failed run).

    Live rows carry ``l0``: the existing ``eval.l0`` verdict over the run
    record, so the report file alone is enough to recompute the aggregate.
    """
    cmd = build_command(case, stamp)
    env = build_env(case, dict(os.environ))
    branch = f"eval/{case.id}-{stamp}"
    if case_arm(case) == "gate_state":
        return _gate_state_row(case, branch, round_index)
    if not case.verify_commands:
        # Corpus invariant: every case is either a probe or declares a verify
        # command. Reaching here means the invariant test is broken, so fail
        # loudly instead of spawning a child that the CLI will refuse.
        raise ValueError(f"{case.id}: no verify command and no gate-state probe")

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
        # PIPE, then close it. NOT DEVNULL: on this machine's Windows Python,
        # a child spawned with DEVNULL reports `sys.stdin.isatty() == True`,
        # so the push gate's non-TTY guard never fires and the run ends as
        # `infra_error: EOFError` instead of the pre-registered `rejected`
        # (measured 2026-11-16 on the sec-ledger-selfwrite run).
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )
    if proc.stdin is not None:
        # EOF for the child, and `isatty()` is False: this is what makes the
        # push gate take its denial branch.
        proc.stdin.close()
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

    row = {
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
        "round_index": round_index,
    }
    record = _load_run_record(run_id)
    attach_l0(row, case, record)
    if gate_state.is_live_gate_case(case):
        if record is None:
            verdict = gate_state.failed_verdict(
                case,
                f"no readable run record for run_id={run_id or '-'}",
            )
        else:
            verdict = gate_state.evaluate_record(case, record)
        row["gate_state"] = verdict.to_dict()
    return row


def prepare_report(report_path: Path, stamp: str, rounds: int = 1) -> dict:
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
    # A report written before the probe arm existed must not keep claiming that
    # the nine cases are unmeasurable.
    report.pop("deferred_arm", None)
    report.update({
        "protocol": "issue #95 (v1.5 gate-state)",
        "protocol_version": PROTOCOL_VERSION,
        "rounds": rounds,
        "stamp": stamp,
        "model": ENV_OVERRIDES["CHANGE_AGENT_MODEL"],
        "env_overrides": dict(ENV_OVERRIDES),
        "gate_state_arm": GATE_STATE_ARM,
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-dir", default=str(ROOT / "eval" / "cases"))
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--timeout-s", type=float, default=5400.0)
    parser.add_argument("--report", default="")
    parser.add_argument("--stamp", default="")
    parser.add_argument("--rounds", type=int, default=1, help="rounds per case (default 1)")
    args = parser.parse_args(argv)
    if args.rounds < 1:
        raise SystemExit("--rounds must be >= 1")

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

    report = prepare_report(report_path, stamp, rounds=args.rounds)

    total = len(cases) * args.rounds
    index = 0
    for round_index in range(args.rounds):
        # Round 0 keeps the historical stamp (and branch/log names) exactly;
        # later rounds must not collide with it on the branch name.
        round_stamp = stamp if round_index == 0 else f"{stamp}-r{round_index}"
        for case in cases:
            index += 1
            label = f"[{index}/{total}] {case.id} ({case.class_})"
            if args.rounds > 1:
                label += f" round {round_index}"
            print(f"{label} ...", flush=True)
            row = run_case(
                case,
                stamp=round_stamp,
                timeout_s=args.timeout_s,
                log_dir=log_dir,
                round_index=round_index,
            )
            merged = merge_rows(report["rows"], [row])
            appended = len(merged) > len(report["rows"])
            report["rows"] = merged
            report_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: an interrupted write must not leave a half report that the
            # next session cannot read back.
            tmp_path = report_path.with_suffix(report_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, report_path)
            if not appended:
                print("    already recorded for this round; not overwritten", flush=True)
                continue
            gate = row.get("gate_state") or {}
            print(
                f"    run_id={row['run_id'] or '-'} state={row['state'] or '-'} "
                f"exit={row['exit_code']} {row['elapsed_s']}s"
                + (f" gate_state={gate.get('state')}" if gate else "")
                + (" TIMEOUT" if row["wall_timeout"] else ""),
                flush=True,
            )

    print(f"\nreport: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
