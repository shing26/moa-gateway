# Contract — issue #80 v2, PART A v2: eval case schema + JSONL loader

The binding contract for this delivery is the `$goal` string passed to
`delivery run`, stored verbatim in the run record's `request.goal`. It is
written before the run starts, so it is frozen per attempt. This file is a
condensation of it, for the verifier's convenience; where the two differ, the
goal file is authoritative. Nothing material from the goal is absent here; the
goal additionally carries tool-usage rules that bind only the implementer's
process, not the artifact.

Every rejected round produced a new goal revision, each fixing a defect the
previous round shipped; the acceptance criteria below are unchanged across
all of them.

- Base SHA: `3c384d951072ef59d681032dbac5788cdfb5ab51`
- Branch: `<branch recorded in bundle.branch>`
- Run record (current round): `data/delivery-runs/dlv-442066965af6445f9794bc907b0e841c.json`
  (round v6; superseded rounds: `dlv-c8e1fa90904b46ce9bdf484f8de94c60` = v2,
  `dlv-b894c70a08e34842ba154be574d48a7e` = v5)
- Goal files: `.scratch/goal-80a-v2.txt` (v2) … `.scratch/goal-80a-v6.txt` (v6),
  `.scratch/goal-80a-v7.txt` (v7, current).

Part B (the L0 deterministic checker) is a SEPARATE delivery and is out of
scope here: `eval/l0.py` and `tests/unit/test_eval_l0.py` must not exist.

## Allowed paths

Exactly these three NEW files. No existing file may be modified, no other file
created, no dependency added.

```
eval/__init__.py
eval/schema.py
tests/unit/test_eval_schema.py
```

## Verification command

```
python -m pytest tests/unit/test_eval_schema.py -q
```

CI also runs `uv run ruff check .` over the whole tree, so lint must be clean.

## Acceptance criteria

1. `eval/__init__.py` exists and holds only a newline. (`write_file` always
   appends a trailing newline, so a zero-byte file is impossible; a one-byte
   file is the intended result.)
2. `eval/schema.py` defines a frozen dataclass `EvalCase` with exactly these
   fields, types and defaults. Declaration order: a plain dataclass forbids a
   defaulted field before a non-defaulted one, so the six required fields come
   first, then the defaulted ones. `kw_only` and `field()` are not authorised.
   The fields are:
   `id: str`, `class_: str`, `source: str`, `base_sha: str`,
   `fixtures: tuple[str, ...] = ()`, `env: tuple[tuple[str, str], ...] = ()`,
   `goal: str`, `allowed_paths: tuple[str, ...]`,
   `forbidden_paths: tuple[str, ...] = ()`, `verify_commands: tuple[str, ...] = ()`,
   `oracle_type: str`, `oracle_checks: tuple[str, ...] = ()`,
   `expected_failure_to_avoid: str = ''`, `tags: tuple[str, ...] = ()`.
   Each field carries a `#:` comment in the MemoryPolicy style. `class_` is one
   of `business`/`failure`/`boundary`/`security`; `oracle_type` is one of
   `script`/`gate_state`/`llm_judge`.
3. `load_cases` is a MODULE-LEVEL function `load_cases(path: str) -> list[EvalCase]`,
   not a method or staticmethod of `EvalCase`.
4. `load_cases` reads JSONL (one object per line, blank lines skipped). For each
   missing required field among `id, class_, goal, base_sha, allowed_paths,
   oracle_type` it raises `ValueError` naming the line number and the field.
   Every field is read through an existence check, so a missing key raises
   `ValueError`, never `KeyError`. Optional fields use `.get(...)`.
5. Unknown `class_` and unknown `oracle_type` values raise `ValueError`.
6. Every tuple-typed field is normalised, with TWO distinct shapes:
   - `tuple[str, ...]` for `allowed_paths`, `fixtures`, `forbidden_paths`,
     `verify_commands`, `oracle_checks`, `tags`. A non-empty JSON list must
     survive as the corresponding tuple and must NEVER come back `()`.
   - `tuple[tuple[str, str], ...]` for `env` only. JSON `[["A","1"]]` becomes
     `(("A","1"),)`; the inner element is a tuple and mutating it raises
     `TypeError`.
7. `tests/unit/test_eval_schema.py` writes each JSONL fixture to a real FILE
   path under `tmp_path`, never a directory, and actually writes the bad row
   before asserting a `ValueError`. It covers: minimal valid round-trip; each of
   the six missing fields raising `ValueError` with the line number; unknown
   `class_`; unknown `oracle_type`; blank lines skipped and a bad row on line 4
   reporting line 4; env as tuple of tuples; optional defaults; frozen
   assignment raising `FrozenInstanceError`.
8. The verification command exits 0 and `ruff check .` is clean.
9. Nothing outside the three allowed paths was created or modified.

## Forbidden

- Modifying any existing file, creating any file outside the three allowed
  paths, or adding a dependency.
- Weakening or deleting an assertion to make the suite pass; skipping a test;
  disabling lint.
- Leaving a failing assertion in place with a "may need adjustment" comment.
- Adding part B (`eval/l0.py`, `tests/unit/test_eval_l0.py`) in this delivery.

## Amendments (factual corrections, no criterion relaxed)

1. **AC2 and AC4 say "six required fields" but list seven.** The seven names
   (`id`, `class_`, `source`, `goal`, `base_sha`, `allowed_paths`,
   `oracle_type`) are correct and `source` IS required. The word "six" is a
   leftover from an earlier draft that predated `source`; read it as "seven".
2. **AC7 says "a bad row on line 4 reporting line 4".** The goal file says
   line 3, and line 3 is the physically correct number for the fixture the
   goal prescribes (blank line, valid row, bad row). Read AC7 as line 3.
3. **Added after v7 was launched, in response to the v6 verifier report:**
   `env` must be annotated exactly `tuple[tuple[str, str], ...]`; no
   `# type: ignore` may appear in any delivered file; no unused imports,
   variables, helpers or classes. These restate goal text already present in
   v6/v7 (`No unused imports, variables, or helpers`) rather than adding new
   requirements — the v6 artifact satisfied the runtime behaviour but widened
   the `env` annotation and suppressed the mismatch.
