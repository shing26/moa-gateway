# Contract — delivery-watchdog-v1: run heartbeat + stale-launch lock

Binding: this file freezes scope before implementation.

## Background (measured, from dogfood rows 13 and the 2026-11-12 复盘)

Two related observability holes: (a) during implementation the run record is
not updated until the attempt ends, and litellm's keep-alive connection makes
"silently working for 10 minutes" and "dead for 10 minutes" look identical —
operators killed 7+ healthy runs on 2026-11-12 because of this; (b) delivery
processes have no watchdog: two launches from 2026-11-12 were found still alive
22 hours later holding live connections, because nothing detects or prevents an
abandoned duplicate launch.

## Allowed paths

Exactly these; nothing else created or modified:

```
apps/code_review_pipeline/delivery/heartbeat.py   (new)
apps/code_review_pipeline/delivery/lockfile.py    (new)
apps/code_review_pipeline/delivery/flow.py        (start/stop heartbeat around run)
app/cli.py                                        (acquire lock in cmd_delivery_run)
tests/unit/test_delivery_watchdog.py              (new)
```

## Verification commands

```
python -m pytest tests/unit/test_delivery_watchdog.py -q
python -m pytest tests/unit -q
python -m ruff check .
```

## Acceptance criteria

1. `heartbeat.py`: a `Heartbeat` handle with
   `start(path: Path, interval_s: float) -> Heartbeat` (classmethod or factory),
   writing `datetime.now(timezone.utc).isoformat()` to `path` ATOMICALLY
   (temp file + `os.replace`) immediately and then every `interval_s`
   (default from `DELIVERY_HEARTBEAT_S`, fallback 30), plus
   `stop()` (deletes the file, cancels the task) and a module function
   `read_age_s(path: Path, *, now: datetime | None = None) -> float | None`
   (None when missing/corrupt). The writer must be a single asyncio task and
   must never propagate an exception into the flow — a failed heartbeat write
   is logged, not raised.
2. `flow.py`: `DeliveryFlow.run` starts the heartbeat at
   `<record_dir>/<run_id>.heartbeat` right after the run record is first
   created and stops it in a `finally` covering the WHOLE run (every exit path:
   finished, failed, rejected, exception). The existing `_move` record writes
   are unchanged. No daemon threads; only asyncio.
3. `lockfile.py`: `acquire(path: Path) -> LockHandle | StaleLockReplaced`
   semantics — the lock file holds JSON `{"pid": int, "started_at": iso,
   "host": gethostname()}`. If the file exists and its pid is ALIVE (check via
   `os.kill(pid, 0)` guarded for Windows: use `ctypes` `OpenProcess`/or
   `psutil`-free approach — on Windows use
   `ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, ...)`;
   must be unit-testable by injection, e.g. an `alive: Callable[[int], bool]`
   parameter defaulting to the real check) → refuse (return None + holder info
   for the CLI to print). If the pid is dead → steal: overwrite with the new
   holder and report that a stale lock was replaced (with the old contents).
   `release()` deletes the file only if it still holds OUR pid.
4. `app/cli.py` `cmd_delivery_run`: acquire
   `<delivery_run_record_dir>/.locks/delivery-<owner>-<name>.lock` before doing
   any work; on refusal print the holder (pid, started_at) and exit 3; release
   in `finally` on every path. A stolen stale lock prints a one-line warning
   naming the replaced holder.
5. Unit tests in `tests/unit/test_delivery_watchdog.py`:
   - heartbeat writes, updates within interval, `stop()` deletes the file;
   - `read_age_s` with an injected `now`: fresh / stale / missing / corrupt;
   - a failing write (read-only dir or monkeypatched `os.replace`) does not
     raise out of the heartbeat task;
   - lock: refuse when `alive` returns True (inject a fake), steal + warning
     payload when False, release removes only our own lock, release after
     someone else stole is a no-op;
   - flow integration (light): run `DeliveryFlow.run` with the existing fake
     ports pattern used by flow tests, assert the heartbeat file exists during
     the run and is gone after (if the flow tests make this hard, a
     `start`/`stop` called-around-implementer assertion via a spy is
     acceptable — say which you did).
6. Full `tests/unit` green; `ruff check .` clean; no suppressions; the flow's
   existing tests are NOT weakened (they may be extended for the spy).

## Forbidden

- Threads (asyncio only), daemon or otherwise.
- Any path where a heartbeat/lock failure crashes or aborts a delivery run.
- Changing the run record schema or the merge state machine.
- Touching `change/**` (task pipeline-gates-v1 owns it) or
  `delivery/resume.py` (task delivery-resume-v1 owns it).
