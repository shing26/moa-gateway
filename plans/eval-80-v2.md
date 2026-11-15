# Contract — issue #80 v2: eval case schema + L0 deterministic checker

Frozen before implementation. This file is a verbatim transcription of the `$goal`
string in `.scratch/run-eval-framework-delivery.ps1` that was passed to
`delivery run` for run `dlv-4862bf4a321045dcaf940a91782c43ef`. It was NOT edited
after the run started.

- Base SHA: `3c384d951072ef59d681032dbac5788cdfb5ab51`
- Branch: `delivery/eval-framework-20261112`
- Run record: `data/delivery-runs/dlv-4862bf4a321045dcaf940a91782c43ef.json`
- Bundle hash: `e58862d4fcf2b8a8c88efa7bca5b39c153fdabfdf8e5838f4f42929ab28b8f64`

## Allowed paths

Exactly these five NEW files. No existing file may be modified, no other file
created, no dependency added.

```
eval/__init__.py
eval/schema.py
eval/l0.py
tests/unit/test_eval_schema.py
tests/unit/test_eval_l0.py
```

## Verification commands

```
python -m pytest tests/unit/test_eval_schema.py tests/unit/test_eval_l0.py -q
```

## Acceptance criteria

1. `eval/__init__.py` exists and is empty.
2. `eval/schema.py` defines a frozen dataclass `EvalCase` with fields:
   `id: str`, `class_: str`, `source: str`, `base_sha: str`,
   `fixtures: tuple[str, ...] = ()`, `env: tuple[tuple[str, str], ...] = ()`,
   `goal: str`, `allowed_paths: tuple[str, ...]`,
   `forbidden_paths: tuple[str, ...] = ()`, `verify_commands: tuple[str, ...] = ()`,
   `oracle_type: str`, `oracle_checks: tuple[str, ...] = ()`,
   `expected_failure_to_avoid: str = ''`, `tags: tuple[str, ...] = ()`.
   `class_` is one of `business`, `failure`, `boundary`, `security`;
   `oracle_type` is one of `script`, `gate_state`, `llm_judge`.
3. `load_cases(path)` reads JSONL (blank lines skipped) and raises `ValueError`
   naming the line number and the field for each missing required field among
   `id, class_, goal, base_sha, allowed_paths, oracle_type`. Unknown `class_` or
   `oracle_type` values also raise `ValueError`. A missing key must never raise
   `KeyError`.
4. `eval/l0.py` defines frozen `L0Check(name, passed, detail)` and
   `L0Report(checks)` with an `all_passed` property, and
   `check_run(record, case) -> L0Report` which must NEVER raise, for any record
   shape in the corpus.
5. `check_run` evaluates, in order:
   - `changed_paths_match` — path read from each `record['bundle']['files']`
     entry via `entry['path']`; every path must be in `case.allowed_paths`.
     bundle null / files missing / files empty → FAIL naming which.
   - `verify_exit_zero` — `record['verification']['exit_code'] == 0`;
     verification null or exit_code missing/None → FAIL.
   - `forbidden_paths_touched` — no `case.forbidden_paths` path among changed
     paths; vacuous pass with detail when the case declares none.
   - `test_files_append_only` — for each changed path under `tests/`, locate the
     patch section whose `diff --git` header names it, FAIL if any line starts
     with `-` but not `---`. No tests/ path changed → vacuous pass with detail.
   - `read_ratio` — count tool calls across all `attempts[].steps[]`, skipping
     empty tool names; reads = `read_file` + `search`; PASS iff `2 * reads <
     total`. Zero counted calls is a FAIL. Detail carries the raw numbers on
     PASS and on FAIL.
   - `verify_cycles` — count `run_tests` + `run_unittest`; PASS at most 5;
     detail carries the count on PASS and on FAIL.
6. The two test files exist, cover the listed cases, and the verification
   command exits 0.
7. The checker grades all real run records without raising, and a record with a
   null bundle never reports `all_passed`.

## Forbidden

- Modifying any existing file.
- Creating any file outside the five allowed paths.
- Adding a dependency.
- Weakening or deleting an assertion to make the suite pass.
- Leaving a failing assertion in place with a "may need adjustment" comment.

## Measured API facts (the contract these were measured from)

Surveyed over 86 real records in `data/delivery-runs/`:

- `bundle` null in 65/86; `verification` null in 59/86; 23/86 have zero steps.
- `bundle.files` is a list of OBJECTS `{"path","kind","before_hash","after_hash","mode"}`,
  never bare strings. `kind` ∈ {added, modified, deleted}.
- `bundle.patch` is a unified diff; each section starts with `diff --git a/... b/...`;
  a deleted file shows `+++ /dev/null`.
- `verification` = `{command: list[str], exit_code: int, output_hash, output_excerpt}`.
- `attempts[]` = `{role, status, summary, stop_reason, usage, steps}`.
- `steps[]` = `{index, action, tool, arguments, observation, outcome, elapsed_ms}`;
  `tool` and `outcome` may be `""`.
- Tool names observed: `read_file`, `search`, `run_tests`, `run_unittest`,
  `replace_in_file`, `list_files`, `git_diff`, `write_file`.
