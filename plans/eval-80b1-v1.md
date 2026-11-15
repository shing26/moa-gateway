# Contract — issue #80 v2, PART B1: L0 checker module (single file)

Binding contract: `.scratch/goal-80b1-v1.txt`, passed as `$goal` to
`delivery run`, stored verbatim in `request.goal`, written before the run and
frozen. This file condenses it for the verifier; the goal file is
authoritative.

Supersedes `plans/eval-80b-v1.md` scope-wise: after run
`dlv-d659d996956b4063a5820f43a030b6df` (150-step re-read spiral), the retry
`dlv-ac6ee67113884eccbd252ac9b8579681` (endpoint hang, no progress for 36 min)
and `dlv-ce8ea4b0990d4bb48ccd8e8e582503c3` (6-step explore-then-finish, verify
exit 4), the two-file Part B is split into B1 (this contract: `eval/l0.py`)
and B2 (a later contract: `tests/unit/test_eval_l0.py`). The checker criteria
below are carried over from `plans/eval-80b-v1.md` unchanged.

- Base SHA: `802dc9a6e0b658e6e9fdb9c469a5757c89633e9a` (PR #83 merged)
- Branch: `delivery/eval-l0-checker-b1-20261113` + the delivery's own suffix

## Allowed paths

Exactly ONE new file. Nothing else created, no existing file modified, no
dependency added, and `tests/unit/test_eval_l0.py` must NOT exist.

```
eval/l0.py
```

## Verification command

```
python -c "import eval.l0"
```

An import smoke: proves syntax validity and that `from eval.schema import
EvalCase` resolves at import time. The behavioural gate is the acceptance
harness (`.scratch/probe-l0.py`, validated against a reference implementation)
plus the independent verifier — the same division of labour as Part A.

CI also runs `uv run ruff check .`. This repo's config ignores
F401/F841/RUF100, so the verifier additionally runs

```
python -m ruff check --isolated --select F401,F841,RUF100 eval
```

and dead code is a FAIL even when CI lint is green.

## Acceptance criteria

1. `eval/l0.py` contains a real `from eval.schema import EvalCase` import that
   is referenced at runtime, not only under `from __future__ import
   annotations`.
2. Frozen dataclasses `L0Check(name: str, passed: bool, detail: str)` and
   `L0Report(checks: tuple[L0Check, ...])`; `L0Report.all_passed` is
   `all(c.passed for c in self.checks)`.
3. `check_run(record: dict, case: EvalCase) -> L0Report` evaluates exactly six
   checks, one `L0Check` each, in this order: `changed_paths_match`,
   `verify_exit_zero`, `forbidden_paths_touched`, `test_files_append_only`,
   `read_ratio`, `verify_cycles`.
4. `check_run` NEVER raises: `record=None`, `bundle` a string/int,
   `verification` a string, `steps` containing non-dicts, `files` containing
   non-dicts all produce a report. `isinstance` precedes every `.get` or
   subscript. Each check has its own try/except appending a failing
   `L0Check` on an unexpected error.
5. `changed_paths_match`: every `entry['path']` (dict entries only) must be in
   `case.allowed_paths`. Three DISTINCT details: bundle not a dict →
   `'bundle is null'`; `files` absent/not a list → `'files missing'`;
   `files == []` → `'files is empty'`.
6. `verify_exit_zero`: `verification.exit_code == 0`; null/non-dict
   verification or missing/`None` exit code → FAIL.
7. `forbidden_paths_touched`: no changed path matches a declared forbidden
   path; empty `case.forbidden_paths` → PASS with a detail saying none are
   declared.
8. `test_files_append_only`: FAIL if any line of `_patch_section(patch, path)`
   for a changed `tests/` path starts with `-` but not `---`. No `tests/`
   path → PASS saying so. ONE shared `_patch_section` helper (no second
   scanner), section runs up to but not including the next `diff --git `
   header, and INCLUDES the section's last line — a `-` line that is the last
   line of a non-final tests section must still be caught.
9. `read_ratio`: tool calls across `attempts[].steps[]`, skipping empty or
   non-string `tool`; `reads = read_file + search`; PASS iff `2 * reads <
   total` (strict). `reads=1,total=2` FAILS; `reads=1,total=3` PASSES; zero
   counted calls FAILS. Detail carries the raw numbers on PASS and FAIL.
10. `verify_cycles`: `run_tests + run_unittest` ≤ 5 PASS, 6 FAIL; detail is
    `run_tests_calls={n}, run_unittest_calls={m}` on both.
11. `ruff check .` clean; no unused imports/variables/helpers (forced ruff
    gate above); no `# type: ignore` or any suppression.
12. Nothing outside `eval/l0.py` was created or modified.

## Forbidden

- Creating `tests/unit/test_eval_l0.py` (that is B2) or any other file;
  modifying any existing file; adding a dependency.
- Weakening an assertion, skipping a test, disabling lint, or any
  `# type: ignore` suppression.
- Two section scanners instead of the one shared `_patch_section`.
- Unused imports, variables, helpers or classes, even though repo lint
  ignores them.
