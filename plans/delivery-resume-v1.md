# Contract — delivery-resume-v1: `delivery resume` re-entry command

Binding: this file freezes scope before implementation.

## Background (measured, from dogfood row 13)

The push gate lives inside the `delivery run` process; when that process dies
(session restart, terminal close, crash), a `ready_for_push_approval` record is
stranded — the pipeline has no way back in, and the only recovery today is a
hand-written one-off script (`.scratch/repush-b2.py`, proven on PR #87). This
task promotes that path into a first-class command. It is recovery ONLY: it
must never re-run the implementer, never rebuild the bundle from a workspace,
and never weaken the human gate.

## Allowed paths

Exactly these; nothing else created or modified:

```
apps/code_review_pipeline/delivery/resume.py    (new: testable logic)
app/cli.py                                      (wire `delivery resume` subcommand)
tests/unit/test_delivery_resume.py              (new)
```

## Verification commands

```
python -m pytest tests/unit/test_delivery_resume.py -q
python -m pytest tests/unit -q
python -m ruff check .
```

## Acceptance criteria

1. `resume.py` exposes `async def resume_run(run_id: str, *, store, github,
   merge_store, settings, approval_port) -> ResumeOutcome` (names may adjust to
   the codebase's idioms; the shape may not). Dispatch on the loaded record's
   state:
   - `ready_for_push_approval` → the gate re-entry path (criterion 2);
   - `waiting_ci`, `awaiting_merge_approval`, `merging` → reconcile the record
     against the merge store (the same `store.reconcile`/`sync_from_merge`
     machinery `delivery view` uses) and return its JSON for printing;
   - any other state → `ResumeOutcome(refused=..., reason=...)`; the CLI exits
     2 and the record is untouched.
2. Gate re-entry path, in order, all mandatory:
   a. Load the record; rebuild the `ChangeBundle` from the RECORD (never from a
      workspace) and assert its recomputed `bundle_hash` equals the recorded
      one; refuse otherwise (exit 2, record untouched).
   b. Re-materialize the workspace exactly as `DeliveryFlow`'s provisioner
      does (tarball at base_sha + git baseline), apply the recorded patch with
      `git config core.autocrlf false` FIRST (the implementer wrote LF bytes;
      autocrlf would drift them to CRLF and the publisher would correctly
      refuse), then rebuild the bundle from the workspace and require equality
      with the recorded bundle hash; refuse (exit 2) on mismatch.
   c. Present the SAME `TerminalPushApprovalPort` gate (`run_id`, branch,
      bundle hash, patch hash, verify exit) — the operator identity and
      `APPROVE` are typed by the human in the TTY. Non-TTY stdin is denied by
      the port already; on denial exit 1 with "approval denied; nothing
      pushed" and leave the record in `ready_for_push_approval`.
   d. On approval: transition the record PUSHING → publish through
      `GitHubChangeAdapter.publish` (the audited path, which re-derives the
      bundle from the workspace and registers the merge record) → record to
      `waiting_ci` with the push outcome, saved via the run store. This mirrors
      `DeliveryFlow._move`'s writes.
3. `app/cli.py` gains subcommand `delivery resume --run-id RUN_ID` with the
   same construction pattern as `cmd_delivery_run` (build github client, merge
   store, run store; close them in finally). Exit codes: 0 pushed/reconciled,
   1 approval denied, 2 refused/infra error.
4. Unit tests (fakes for github/merge_store/adapter — no network):
   - state dispatch: each of the three branches; refused states listed;
   - bundle-hash mismatch refusal (tamper the record's bundle_hash);
   - non-TTY denial leaves state unchanged and returns exit-1 outcome;
   - happy path with a fake adapter: publish called with the recorded bundle,
     record ends `waiting_ci`, push outcome persisted;
   - the autocrlf regression: patch applied to a fresh workspace must reproduce
     the bundle hash (use a tmp git repo fixture; commit an LF file's patch,
     apply with autocrlf unpset-default and with false, show the difference is
     why the flag is required) — if flaky on this machine, assert the command
     list instead.
5. Full `tests/unit` green; `ruff check .` clean; no suppressions.

## Forbidden

- Any flag that auto-approves, reads the approval from argv/env, or skips the
  TTY prompt (the gate stays human-only; non-TTY is always denied).
- Re-running the implementer or the verify command (the bundle is frozen
  evidence; `publish` re-checks it via `_assert_bundle_current`).
- Writing the bundle from the re-materialized workspace into the record.
- Modifying the merge state machine, `flow.py`, `change/**`, or `run_store.py`.
