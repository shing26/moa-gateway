# Contract — issue #80 v2, PART B: L0 deterministic checker

Binding contract: the `$goal` string passed to `delivery run`, stored verbatim
in the run record's `request.goal`, written before the run starts and frozen
per attempt. This file is a condensation for the verifier's convenience; where
they differ, the goal file is authoritative.

Goal revisions so far (the acceptance criteria below are identical in both):
- `.scratch/goal-80b-v1.txt` — run `dlv-d659d996956b4063a5820f43a030b6df`,
  `budget_exhausted` at 150 steps. 65 of 89 reads re-read its own test file;
  it wrote both files but never converged.
- `.scratch/goal-80b-v2.txt` — current. Changed only section "Work order"
  (one write per file, no re-reading own files, short shared fixtures, finish
  on verify exit 0) and raised `DELIVERY_MAX_STEPS` 150→220 in the launcher.
  No acceptance criterion, allowed path, or verification command changed.

- Base SHA: `9741c1f085cb06f92cfa6c2e8ab4042e6dad1e50` (PR #82 merged; Part A
  `eval/schema.py` is on main)
- Branch: `delivery/eval-l0-checker-20261112` + the delivery's own suffix
- Part A was a SEPARATE delivery; `eval/__init__.py`, `eval/schema.py` and
  `tests/unit/test_eval_schema.py` must NOT be modified here.

## Allowed paths

Exactly these two NEW files. Nothing else created, no existing file modified,
no dependency added.

```
eval/l0.py
tests/unit/test_eval_l0.py
```

## Verification command

```
python -m pytest tests/unit/test_eval_l0.py -q
```

CI also runs `uv run ruff check .`; lint must be clean. This repo's ruff
config ignores F401/F841/RUF100, so the verifier additionally runs

```
python -m ruff check --isolated --select F401,F841,RUF100 eval tests/unit/test_eval_l0.py
```

and dead code is a FAIL even when CI lint is green.

## Acceptance criteria

1. `eval/l0.py` begins with a real `from eval.schema import EvalCase` import.
2. Frozen dataclasses `L0Check(name, passed, detail)` and
   `L0Report(checks: tuple[L0Check, ...])`; `L0Report.all_passed` is
   `all(c.passed for c in self.checks)`.
3. `check_run(record: dict, case: EvalCase) -> L0Report` evaluates exactly six
   checks, in this order: `changed_paths_match`, `verify_exit_zero`,
   `forbidden_paths_touched`, `test_files_append_only`, `read_ratio`,
   `verify_cycles`. One `L0Check` per check, always.
4. `check_run` NEVER raises. `record=None`, `bundle` a string or int,
   `verification` a string, `steps` containing non-dicts, `files` containing
   non-dicts all produce a report. `isinstance` checks precede every `.get` or
   subscript; `is not None` alone is insufficient. Each check has its own
   try/except that appends a failing `L0Check` on an unexpected error.
5. `changed_paths_match`: read `entry['path']` from each dict in
   `bundle.files` (skip non-dicts); every path must be in `case.allowed_paths`.
   Three DISTINCT details: bundle not a dict → `'bundle is null'`; `files`
   absent/not a list → `'files missing'`; `files == []` → `'files is empty'`.
6. `verify_exit_zero`: `verification.exit_code == 0`. Null verification, a
   non-dict verification, or a missing/`None` exit code → FAIL.
7. `forbidden_paths_touched`: no changed path equals a declared forbidden path.
   Empty `case.forbidden_paths` → PASS with a detail saying none are declared.
8. `test_files_append_only`: for each changed path starting `tests/`, inspect
   `_patch_section(patch, path)` and FAIL on any line starting `-` but not
   `---`. No `tests/` path → PASS saying so. ONE shared `_patch_section`
   helper, whose section runs up to (not including) the next
   `diff --git ` header — no look-ahead, and it must include the section's
   LAST line. A `-` line that is the last line of a non-final tests section
   must still be caught (regression test).
9. `read_ratio`: count tool calls across `attempts[].steps[]`, skipping empty
   or non-string `tool` values; `reads = read_file + search`; PASS iff
   `2 * reads < total` (strictly fewer than half). `reads=1,total=2` FAILS;
   `reads=1,total=3` PASSES; zero counted calls is a FAIL. The detail carries
   the raw numbers on both PASS and FAIL.
10. `verify_cycles`: `run_tests + run_unittest` ≤ 5 PASS, 6 FAIL. Detail is
    `run_tests_calls={n}, run_unittest_calls={m}` on PASS and FAIL.
11. `tests/unit/test_eval_l0.py` fixtures use the REAL record shape:
    `bundle.files` entries are dicts with `path/kind/before_hash/after_hash/
    mode`, never bare strings; patch literals are module-level constants or
    `textwrap.dedent` so diff lines start at column 0. It covers all cases
    listed in the goal, including the all-six-pass record with steps that make
    `read_ratio` pass, the three distinct changed-paths details, every
    never-raises case, and the 5/6 verify-cycle boundary.
12. The verification command exits 0 and both ruff gates are clean.
13. Nothing outside the two allowed paths was created or modified.

## Forbidden

- Modifying any existing file, creating any file outside the two allowed paths,
  or adding a dependency.
- Weakening or deleting an assertion to make the suite pass; skipping a test;
  disabling lint; writing `# type: ignore` or any other suppression.
- Leaving a failing assertion in place with a "may need adjustment" comment.
- A test whose name/docstring claims coverage its body does not exercise — in
  particular a test that only asserts its own fixture.
- Unused imports, variables, helpers or classes, even though repo lint ignores
  them.
- Two section scanners instead of the one shared `_patch_section` helper.
