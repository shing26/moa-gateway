# Contract — issue #80 v2, PART B2: test file for the L0 checker (single file)

Binding contract: `.scratch/goal-80b2-v1.txt` (frozen per attempt; this file
condenses it for the verifier, the goal is authoritative). Split from
`plans/eval-80b-v1.md` together with B1 after the two-file Part B failed three
times; criteria 11 of `plans/eval-80b-v1.md` moved here, unchanged in
substance.

- Base SHA: `2f05e4f2c4898af298370807ecf84e59b4a2dced` (PR #85 merged)
- Branch: `delivery/eval-l0-tests-b2-20261113` + the delivery's own suffix
- Prerequisite: B1 merged — `eval/l0.py` with `L0Check`, `L0Report`,
  `check_run` is on main.

## Allowed paths

Exactly ONE new file. Nothing else created or modified.

```
tests/unit/test_eval_l0.py
```

## Verification command

```
python -m pytest tests/unit/test_eval_l0.py -q
```

CI also runs `uv run ruff check .`. Forced dead-code gate (repo ignores
F401/F841/RUF100):

```
python -m ruff check --isolated --select F401,F841,RUF100 tests
```

## Acceptance criteria

1. Suite exits 0; every test passes.
2. Fixtures use the REAL record shape: `bundle.files` entries are dicts with
   `path/kind/before_hash/after_hash/mode`, never bare path strings.
3. Patch literals are module-level constants or `textwrap.dedent` — diff lines
   start at column 0.
4. Coverage, one test per case:
   - all six checks pass on a conforming record whose steps make read_ratio
     pass (an all-pass fixture cannot have zero steps);
   - changed_paths_match fails on an unlisted path;
   - changed_paths_match fails without raising on bundle null / files missing
     / files empty, with the three details distinguishable;
   - check_run does not raise for record None, bundle a plain string, and a
     steps list containing a non-dict;
   - verify_exit_zero fails on exit 1, on verification missing, on null;
   - forbidden_paths_touched fails when a forbidden path is present and passes
     when none are declared;
   - test_files_append_only PASSES with a real tests/ section of only '+' and
     context lines (the tests path listed in bundle.files — the vacuous branch
     is not this test);
   - test_files_append_only fails on a '-assert' line;
   - test_files_append_only still fails when '-assert' is the LAST line of a
     tests/ section that is NOT the patch's last section (regression for the
     dropped-last-line bug);
   - read_ratio: passes at reads=1,total=3; fails at exactly half
     (reads=1,total=2); fails when reads are the majority; FAILS on zero
     steps; detail carries the numbers when it passes;
   - verify_cycles PASSES at exactly 5 total calls and FAILS at exactly 6;
     counts run_unittest as well; detail names both counts;
   - a realistic record with a null bundle is graded without raising and does
     not report all_passed.
5. Every test that writes fixtures uses pytest tmp_path; no hardcoded
   absolute paths.
6. Each test can fail for the reason its name/docstring claims — no
   fixture-only assertions, no tautology after a stronger assertion.
7. No unused imports/variables/helpers (forced ruff gate); no
   `# type: ignore`.
8. The file is under ~220 lines via one shared record-builder helper (soft
   target; not a gate by itself, but the verifier flags bloat).
9. Nothing outside `tests/unit/test_eval_l0.py` was created or modified.

## Forbidden

- Modifying `eval/l0.py`, `eval/schema.py` or any existing file; creating any
  other file; adding dependencies.
- Weakening or deleting an assertion to pass; skipping a test; disabling lint;
  any `# type: ignore`.
- A test whose name/docstring claims coverage its body does not exercise.
- Unused imports, variables, helpers or classes.

## Amendments (2026-11-13, after the first B2 run)

No criterion relaxed. Recorded from the first B2 artifact (run
`dlv-3f12822f6f9e4f24862d323b4dd807e4`, state `ready_for_push_approval`,
never pushed, gate never opened) and its mutation gate:

1. Harness correction, not a criteria change: criterion 4's "patch literals
   … diff lines start at column 0" is satisfied via `textwrap.dedent`, so
   the checker now inspects the module's runtime string constants instead
   of raw source lines. First run's B2.10 failure was a checker false
   positive; criterion 4 stands unchanged.
2. The delivered `-assert`-last-line regression fixture places `-assert`
   mid-section (a `+new line` follows inside the same section), so it
   cannot catch the dropped-last-line bug its docstring names — the
   mutation gate proved this. Criterion 4's bullet is exercised only when
   `-assert` is the section's FINAL line, followed directly by the next
   section's `diff --git ` header, with at least one later section after
   it. The Forbidden item "a test whose name/docstring claims coverage its
   body does not exercise" applies to the first artifact.
3. Goal v2 adds two cases beyond the original list so the mutation gate is
   fully discriminative: verify_cycles with 5 `run_tests` + 5
   `run_unittest` (sum 10) FAILS — the only case distinguishing the SUM
   rule from a per-counter rule; read_ratio with empty-string tool entries
   (`''` twice, and `''`+`''`+`read_file`) FAILS — total counts only
   non-empty tool strings, per the semantics frozen for `eval/l0.py`.
4. Criterion 7 stands: the first artifact again had exactly one unused
   import (`L0Check`); goal v2 restates it inline.

### Round 2 outcome and goal v3 (same day)

Round 2 (run `dlv-477fff1ecbc8414a9db3204d2be32008`) ended
`budget_exhausted`: attempt 1 hit the 120-step limit with no bundle. Trace:
the first write carried a SyntaxError (`)` mismatching `{` at line 179),
and the goal's "never read a file you just wrote / rewrite at most once"
rules forced incremental `replace_in_file` repair, which needs re-reads —
57 self-reads and 23 `run_tests` calls later the same collection error
stood. Goal design defect, recorded as such. Goal v3 changes only the
repair rules (full-file rewrite up to 3 writes when collection errors
appear, ≤5 self-reads, ≤8 run_tests) and inlines the two fixture-helper
skeletons to cut boilerplate. No acceptance criterion changed.

### Round 3 outcome and goal v4 (same day)

Round 3 (run `dlv-ef325699faca4e2988b246d76decec1b`) also ended
`budget_exhausted` — but the suite went green at step 21 (24 passed, exit
0, three writes, ten self-reads: the v3 repair rules worked). The remaining
99 steps were an endgame loop: the goal's "LINT IS PART OF THE GATE" made
the implementer hunt for a lint tool that does not exist in the run
environment, smuggling `ruff check` through run_tests selectors (43
run_tests, 25 list_files, 5 `bash` calls) until the step limit. Goal v4
demotes lint to a hand-checked hygiene note, states outright that no lint
tool or shell exists in the environment, and makes "suite green → next
action is finish" imperative. No acceptance criterion changed.

### Round 4 outcome, harness corrections, goal v5 (same day)

Round 4 (run `dlv-6ec1b17236a54ae9bc04a90c497d186b`) converged in ONE
attempt of 19 steps, verify exit 0, and all five mutation tests caught.
Three harness findings, two of them checker false positives:

1. B2.9 false positive: the fixtures carry the full real shape
   (`path/kind/before_hash/after_hash/mode`) with single-quoted literals;
   the checker only looked for double quotes. Fixed to accept either.
2. B2.8 false positive: the suite is pure in-memory (no filesystem use), so
   criterion 5's premise ("every test that WRITES fixtures uses tmp_path")
   does not apply; the checker now requires tmp_path only when the module
   uses filesystem APIs, and always requires no hardcoded absolute paths.
3. Genuine: ruff C408 on `_case` — caused by the goal v3 helper skeleton
   itself (`base = dict(...)`, written verbatim by the implementer).
   Checker defect owned by the goal author, not the implementer. Goal v5
   changes only the skeleton to a dict literal (lint-verified in isolation);
   no acceptance criterion changed.

### Round 5 outcome and goal v6 (same day)

Round 5 (run `dlv-8f9cc42b5649455e92ee9acf0fdfd92b`) converged in one
attempt of 33 steps; 14/16. Two residual defects:

1. The `L0Check` unused import recurred a third time despite the goal's
   "check by hand" note. Goal v6 replaces exhortation with a prescription:
   the file's import lines are given verbatim (`from eval.l0 import
   check_run, L0Report`), with L0Check named as the known failure.
2. The last-line regression fixture again fails to discriminate: a blank
   line sits between `-def assert foo` and the next `diff --git` header,
   so the `-assert` line is not the section's final line and the
   dropped-last-line mutation survives. Goal v6 states the fixture shape
   exactly: `-assert` followed DIRECTLY by the next header, no blank line.

No acceptance criterion changed.

### Round 6 outcome and goal v7 (same day)

Round 6 (run `dlv-3b3f08d6cfe24341acf4f1d3ff38ef83`) hit the no-write
give-up mode twice (12 and 10 steps, "model finished", no file written) —
the same regression B1 v3 showed when the goal carried failure-history
framing. Goal v6 had introduced two such framings ("this exact mistake
ended three previous attempts", "this exact mistake happened in the
previous attempt"). Goal v7 keeps both precise rules (verbatim import
lines; `-assert` directly followed by the next header) and strips the
fear framing — matching the recorded B1 lesson that calm inline spec edits
beat warning blocks. No acceptance criterion changed.

## Verifier FAIL → round-8 corrections (2026-11-13)

Independent verifier (fresh `verifier` subagent, own re-runs of all commands)
returned **FAIL** on run `dlv-9f97043dac754c78b731491ca4072659` (bundle
`941728d8…6e00`). Gate tab closed; that artifact was NOT pushed. Findings
and their disposition — no acceptance criterion relaxed:

- **Verification command 3 authoring error (owned by contract author).**
  `python -m ruff check --isolated --select F401,F841,RUF100 tests` scans the
  WHOLE pre-existing `tests/` tree; at base SHA it already reports 141
  errors (48 F401, 3 F841, 90 RUF100) with an identical error-file list
  before and after the patch — the verifier proved this on a pristine
  control clone, and zero of them come from the delivered file. The frozen
  command is kept verbatim; its PASS condition is hereby made explicit:
  run it on BOTH the base SHA (control) and the patched clone; the delivery
  passes iff the error set is identical and contains no error from
  `tests/unit/test_eval_l0.py`. Additionally the file-scoped form
  `… tests/unit/test_eval_l0.py` must exit 0. This is a measurement
  correction, not a narrowing of any criterion.
- **D1**: `test_check_run_no_raise_on_non_dict_steps` used a non-dict
  ATTEMPT, not a non-dict element inside a `steps` list; verifier mutated
  both step-level `isinstance` guards to `raise TypeError` and all 27 tests
  still passed. Criterion 4/6 and Forbidden item 3 stand; round 8 must
  place a non-dict INSIDE `steps`.
- **D2**: the two `verify_exit_zero` tests had identical bodies;
  `_rec()` always emits the `verification` key, so the key-missing case was
  never exercised (verifier mutation: absent key treated as exit 0 → all
  tests still passed). Round 8 must build one record where the key is
  genuinely absent.
- **D3/D5**: docstring overclaims ("three variants" in a one-variant test)
  and a `-assert` docstring over a `-def test_old():` fixture. Forbidden
  item 3 stands; round 8 must make names/fixtures agree.
- **D4**: goal v7 requires TWO empty-tool `read_ratio` fixtures
  (`('', '')` and `('', '', 'read_file')`); only the latter existed.
  Both stand.
- **D6**: 526 lines vs the soft ~220-line target — flagged, not a gate.

Harness upgrades for round 8 (permanent): two more mutation probes —
N1: replace both step-level `if not isinstance(step, dict):` guards with
`raise TypeError`; N2: make a verification-key-absent check pass — each
must make the suite FAIL; plus AST checks for a non-dict element inside
`steps`, a record without the `verification` key, and both empty-tool
fixtures. Goal v8 states the three cases precisely and keeps every
round-2..7 rule.

### Round 8a/8b outcome and goal v9 (same day)

Two v8 runs (`dlv-cd9b277957b64a04b136421804f6eb71`,
`dlv-8c429bb88caf4d79a5676b521e20cdd1`) ended `needs_human` with
no bundle: empty-summary "model finished" no-write attempts (10–38
steps) plus one 3-test-failure attempt left unfixed. The first coincided
with litellm 120s timeouts; the second followed a healthy 12.8s endpoint
probe, so goal length/presentation is the likely factor — v8 appended a
9.0k-char three-rule tail block. Goal v9 (8.6k) inlines exactly the same
three requirements into the case-list bullets they belong to (non-dict
INSIDE steps, key-absent record via `r.pop('verification')`, two separate
empty-tool fixtures, name/fixture agreement) and removes the redundant tail.
No acceptance criterion changed.

### Round 9 outcome and goal v10 — strategy change (same day)

v9 (run `dlv-fd1f7152594949588633086356bddd05`) again produced two
no-write empty-completion attempts (5 and 12 steps). Accreted goals had
grown from 5.4k (v1, converged) to 9.0k; each round appended more
history/lessons. Per the user's "拆更小或换策略" directive, goal v10 is a
STRATEGY CHANGE, not another patch: rebuilt lean from v1 (5.9k), all
eight rounds' lessons represented only as short current rules (whole-file
rewrite on collection errors, green → finish, helper skeletons, exact
import lines, no-lint-tool statement), with every substantive requirement
inlined into the case list — non-dict INSIDE steps, key-absent record via
`r.pop('verification')`, two separate empty-tool tests, 5+5 sum case,
last-line fixture shape, name/fixture agreement. No acceptance criterion
changed; the goal carries no failure history.

### Round 10 outcome, harness upgrades, goal v11 (same day)

v10 (run `dlv-74d1c5e983b94b89bc4194b012d7e0e3`) converged (23+57 steps,
verify exit 0) but the upgraded harness found six substantive defects:
a `pass`-only assertion-free test (PIE790), unused `L0Report` import and
unused `report` variable (F401/F841, hence the base-control gate gained 2
new errors), a mixed empty-tool test whose docstring claims three steps but
whose fixture has two empty steps, an assertion-free no-raise test (N1
survived), and verify_cycles fixtures where `action == tool` everywhere so
counting by action (M4) survived. Real-record `action` is the constant
`call_tool`, never the tool name. Harness upgrades (permanent): AST checks
for assertion-free tests, a record literal genuinely lacking the
verification key, both empty-tool fixture shapes, and a verify_cycles
fixture with `action != tool`; B2.8 now detects filesystem calls via AST
(patch-text strings no longer false-positive); B2.A2 accepts a bare dict
literal as well as `pop`. Regression-tested: the v7 FAIL artifact and the
v10 artifact are both correctly rejected on exactly the substantive gaps.
Goal v11 = v10 + four calm inline rules (every test has at least one
assertion; every named import and assigned variable is used; empty-tool
fixtures have exactly `('', '')` and `('', '', 'read_file')`; verify_cycles
steps carry `action='call_tool'` with tool being run_tests/run_unittest).
No acceptance criterion changed; no verdict from this harness is treated
as adjudication — a fresh verifier will re-run everything.

### Round 11 outcome and goal v12 (same day)

v11 (run `dlv-584e6c3b05a240a5ae873e425ba40196`) converged in ONE attempt
of 13 steps; harness 20/24. All four failures are substantive, confirmed
by reading the delivered file: `test_verify_exit_zero_fails_key_absent`
used `_rec(attempts=[])` but `_rec` ALWAYS emits `verification=None`, so
the key was never absent (N2 survived); verify_cycles steps had NO
`action` field at all, so M4's "catch" was an artifact of `get('action')`
returning None rather than a test of the real constant-action shape
(A5 false); the non-dict-in-steps test asserted only
`isinstance(report, L0Report)` and could not feel step-guard damage (N1
survived). Goal v12 gives three copyable fixture snippets (non-dict steps
plus a `reads=1, total=3` assertion; `rec.pop('verification')`; both
`action='call_tool'` and `tool='run_tests'` on every verify_cycles step).
No acceptance criterion changed; harness unchanged.

### Round 12 outcome and goal v13 (same day)

v12 (run `dlv-b1682684acbf4e36aa0246640563299c`) converged in one 10-step
attempt; harness 21/24. Seven mutation probes ALL caught (M1–M6, N1, N2
included). Three residual findings, all substantive: F841 unused local
`check = _find_check(report, 'all_passed')` (line 96), and both "empty
tool" fixtures using bare `''` strings as non-dict step elements instead of
dicts with `{'tool': ''}` — their docstrings claim tool tuples
`('', '')` / `('', '', 'read_file')` but the bodies do not exercise them
(A3; the base-control gate's +1 error is the same F841). Goal v13 gives
the two fixtures as copyable dict literals and states that every assigned
variable must be used immediately. No acceptance criterion changed;
harness unchanged.

### Round 13 outcome and goal v14 (same day)

v13 (run `dlv-86d8e2ca84084e658b561eafbcbc0149`) converged (68+6 steps,
verify exit 0); harness 22/24, seven of the eight mutation probes caught.
Two linked defects: two patch constants mixed an indented first `diff
--git` header with unindented later headers (dedent leaves the first
indented — B2.10), and the last-line regression fixture again put
`+assert foo` / context lines after `-assert` plus an indented second
header, so the dropped-last-line mutant M1 survived. Goal v14 makes the
fixture shape explicit (two sections, `-assert False` is literally the
first section's final line, next line is `diff --git a/main.py b/main.py`)
and requires uniform source indentation inside every dedent literal. No
acceptance criterion changed; harness unchanged.

### Round 14 outcome and goal v15 (same day)

v14 (run `dlv-cf2ea27b71e545b3a67be96b9e6c5d0f`) hit the no-write
empty-completion mode twice (9 and 4 steps). Goal v14 was 7.6k chars;
convergence history in this contract shows sensitivity above ~7k (v11
6.3k and v12 6.9k converged in one attempt, v13 7.2k in two). Goal v15
(7.0k) compresses the same three rules to copyable shapes with no
explanatory prose. No acceptance criterion changed; harness unchanged.

## Verifier FAIL #2 → round-16 corrections (2026-11-13)

Second fresh verifier (own reconstruction, own re-runs) returned **FAIL**
on v15 (`dlv-5b0e3bdcef914452910f68f57c1aaf38`, bundle `bc3093c0…ece`).
Gate tab closed; artifact NOT pushed. All eight required mutants were
caught, hashes/paths/lint/base-control passed. Findings, all substantive:

- **Detail-only tests (criterion 4/6, Forbidden).** Twelve tests named
  "fails"/"passes" assert only a `detail` string. The verifier flipped the
  checker's `passed` flag at ten exact sites (leaving detail unchanged) and
  the suite stayed green (25 passed). Keep the detail assertions but EVERY
  such test must also assert `check.passed is False/True`.
- **verify_cycles detail untested (criterion 4).** Replacing both
  `detail=f'run_tests_calls={n}, run_unittest_calls={m}'` strings with a
  constant left 25 passed. At least one verify_cycles test must assert the
  detail contains both `run_tests_calls=` and `run_unittest_calls=`.
- **Unused module helper (criterion 7, Forbidden).** `_PATCH_READ_FILE`
  defined, never referenced; ruff cannot see module-level unused constants.
- Goal deviations (recorded, not separately gated): one patch literal
  inline in a test rather than module-level; "realistic null bundle" record
  used `attempts=None` instead of `attempts=[]`; 413 lines vs soft ~220.

Harness upgrades (permanent): P1 flip every `passed=True`→`False` and
`passed=False`→`True` in eval/l0.py simultaneously (suite must FAIL); P2
replace both verify_cycles detail f-strings with a constant (suite must
FAIL); P3 AST check that every module-level name starting `_PATCH` is
loaded somewhere; P4 AST check that every test asserting a `.detail`
substring also asserts `.passed` on the same check object. Goal v16 adds
these four rules as short inline statements; no acceptance criterion
relaxed.

### Round 16 outcome + harness scope correction (same day)

v16 (run `dlv-baec2ca8fcfe41fc9f3662ca134c6d11`) converged (10+24
steps). Harness with verifier-2 probes: 24/27. Diagnostics:

- P1 first implementation flipped ALL 32 `passed=` flags including
  branches the frozen coverage list never names (except-handler fallbacks,
  append_only bundle-null/files-missing, forbidden_paths none-touched).
  Scope corrected to the nine `(check_name, detail)` branches the verifier
  itself probed; those nine flips are ALL caught by v16 — the verifier's
  core "detail-only tests" defect is fixed. P2 (detail suppression) caught,
  A6 (unused patch constant) clean.
- Two linked real defects remain: no non-dict element INSIDE a `steps`
  list (A1) and N1 step-guard mutant survives. Since v15 had this exact
  fixture, this is a stochastic regression; goal v16 is unchanged and
  will be re-run before any new wording is added. No acceptance criterion
  relaxed; P1's rescope aligns the harness with the frozen coverage list,
  not with a weaker verdict.

### Round 16 retries and goal v17 (same day)

Two v16 retries (`dlv-c15b5e2713cb4614a0b0fea1d4736226`,
`dlv-1fc43f31f9de446182b28140d0f65c06`) hit no-write empty completions;
the second followed a healthy 9.2s endpoint probe, so this is goal-length
sensitivity again (v16 7.1k). Goal v17 (6.6k) rebuilds from v15, keeps
all verifier-2 rules as four compact lines, and removes two verbose
snippets/paragraphs. No acceptance criterion changed.

### Rounds 17 retries: model change (same day)

Three consecutive v17/v16-retry runs
(`dlv-5a5f6ca835b0466faa5a74f27e6b15e0`,
`dlv-162ca347d0ca4685b31608a4c346b8ad`,
`dlv-1fc43f31f9de446182b28140d0f65c06`) ended in no-write attempts:
one attempt's summary literally said "Let me create the test file" and the
next step was an empty tool call — truncated tool-call generation, not a
rational stop. Endpoint probes were healthy at 9–13s. Per the user's
"拆更小或换策略" directive, the next run keeps the SAME frozen contract
and goal v17 but switches the implementer model to the already-configured
fallback `mistralai/mistral-large-4-1t-a52b`. No acceptance criterion
changed; the verifier remains independent of the implementer model.

### Round 17 (mistral) outcome and harness correction (same day)

After three nemotron runs produced truncated tool calls (model said
"Let me create the test file" then emitted an empty tool call), switching
the implementer to the configured fallback
`mistralai/mistral-large-4-1t-a52b` converged in ONE 12-step attempt:
run `dlv-0b22f97416084f3bb3ff65d417212145`, bundle `e26d0d22…39fd`,
verify exit 0. Harness initially reported 26/27: A5 false positive —
the verify_cycles fixtures do carry `action='call_tool'` with
`tool='run_tests'/'run_unittest'`, but expressed as `[{...}] * 5`, which
the AST probe did not traverse. AST probe fixed to gather dicts through
list multiplication/addition; v15 FAIL artifact still correctly fails
A6/P1/P2. Final harness: **27/27**, including all eight mutants, nine
contract-branch pass-flag flips, detail suppression, and unused helpers.
No acceptance criterion changed. A THIRD fresh verifier adjudicates next.

## Verifier FAIL #3 → round-18 corrections (2026-11-13)

Third fresh verifier (own reconstruction from the bundle patch, own re-runs,
implementer model independent) returned **FAIL** on v17-mistral
(`dlv-0b22f97416084f3bb3ff65d417212145`, bundle `e26d0d22…39fd`).
Gate tab closed; PR #86 stays unmerged.

Everything substantive passed independently: `git apply` clean, after_hash
`67bd4827…c94` and `bundle_hash` recomputed from the patch, `patch_sha256`
and verify-output hash matched, allowed paths exactly
`{tests/unit/test_eval_l0.py}`, `pytest` exit 0 (26 passed), `ruff check .`
exit 0, forced `--isolated --select F401,F841,RUF100` identical to the
pristine base control (141 = 141, zero errors in the delivered file), all
nine contract-branch pass-flag flips CAUGHT, detail suppression CAUGHT, all
eight required mutants CAUGHT, and every FAIL #1/#2 finding verifiably fixed.

- **Single in-scope defect (criterion 4/6, Forbidden).**
  `test_test_files_append_only_fails_assert_last_line` (line 248) asserts
  only `check.passed is False` and never asserts `check.detail`. The frozen
  goal v17 rule is: every test named `fails`/`passes` asserts BOTH the
  passed flag and its detail. This is the only one of 26 tests with the gap
  (19 assert both, this one flag-only; no test is detail-only). The branch's
  detail is covered by the sibling `..._fails_assert` test, so it is a
  formal gap rather than a coverage hole — but the frozen rule is unmet and
  the criterion is not relaxed.
- Recorded non-gated deviations that persist (not criteria): one patch
  literal inline in a test rather than module-level or `dedent` (diff lines
  still at column 0); the "realistic null bundle" record uses
  `attempts=[{...}]` instead of `attempts=[]`; 353 lines vs the soft ~220
  target.
- Out-of-scope mutants the verifier seeded and the suite does not catch
  (search-counting in read_ratio, non-dict file entries in
  changed_paths_match, path-agnostic section matching, forbidden bundle-null
  flip). None is named by criteria 1–9 or the frozen coverage list; they are
  recorded here as candidate coverage for a future contract, not as FAILs.

Harness upgrade (permanent): **A7** — the AST probe now derives, per test,
which assertion styles its body carries (flag-pass / flag-fail / detail) and
fails any test whose name contains `_fails_` or `_passes_` while missing the
matching flag assertion or the detail assertion. This closes the blind spot
A4/vacuous and P1 leave open: a flag-only test survives P1 whenever a sibling
covers the same branch. The upgraded probe reproduces the verifier's finding
exactly (A7 reports precisely `test_test_files_append_only_fails_assert_last_line`),
and a regression sweep over all stored artifacts keeps its discrimination:
v16 = 0 gaps, v17m = 1, v13 = 7, v12 = 7, v11 = 6, v4 = 8, v5 = 1.
Harness is now 28 criteria; v17m scores 27/28, the one FAIL being A7.

Goal v18 keeps the frozen contract and adds one sentence requiring the
detail assertion on every `fails`/`passes` test (it was already implied by
the BOTH rule, which the implementer applied to 25 of 26 tests). No
acceptance criterion relaxed, no goal rule removed.

### Rounds 18a–18d: transport, then a weaker artifact (2026-11-13)

Four launches before one produced a usable artifact. All four keep the SAME
frozen contract and goal v18; only the transport/launch changed.

- **18a (mistral).** `dlv-1c4d3ea7…`, 7 x 120s `litellm.Timeout`, `llm_calls=0`,
  no attempt recorded. Endpoint probe with the REAL delivery payload failed the
  same way at 180s; a minimal `max_tokens=8` call to the same model returned in
  10.4s. Not a network fault (codeload and api.github.com both 200 in 0.6–1.8s).
- **18b (poolside).** `dlv-d08d2102…`, 2 x 120s timeouts, no attempt. But the
  same goal payload through the same probe returned in 8.8–9.1s, and the FULL
  implementer path (`ProductionImplementer.run` with the real tool registry, same
  goal, 4-step cap) completed 5 LLM calls in 14.3s. So the endpoint is healthy and
  the timeout lands specifically on the single call that must emit the whole
  ~350-line file.
- **18c (poolside, `LLM_TIMEOUT=300`).** Converged. `dlv-d141a5f5…`, bundle
  `681a181c…d420`, verify exit 0. Harness: **24/28** — a WEAKER artifact than the
  rejected v17m, and it fails on four independent criteria:
  - B2.6 / B2.12: `import textwrap` unused (F401; ruff cannot see it under the
    repo's ignore list), so the forced gate reports 1 new error vs the pristine
    base control (141 -> 142).
  - B2.A5: no verify_cycles fixture carries `action` alongside `tool`, so M4/N3
    are only caught by accident.
  - B2.A7: `test_verify_cycles_passes_five` and `test_verify_cycles_fails_six`
    assert `passed` but not `detail` — the exact FAIL #3 defect, twice.
  - Only 19-20 tests / 172 lines vs 26 tests for v17m: coverage regressed.
  Gate tab closed, nothing pushed.

Conclusion for the contract: `LLM_TIMEOUT` must be >= 300s for this repo's
whole-file deliveries, and the transport question is settled — the earlier
timeouts were generation latency on one large tool call, not connectivity.
Goal v18's wording is not implicated (18c converged in one attempt on it), but
v18 alone does not force the `action` key or the `textwrap` usage, so those two
are added as explicit goal lines in v19 rather than left to inference.

### Round 19 outcome (2026-11-13)

`dlv-63f4ecea…`, bundle `54fe7b43…2af5`, goal v19, implementer poolside
(`LLM_TIMEOUT=300`), one 12-step attempt, verify exit 0, 26 tests / 30 test
functions counted by the probe. Local harness: **28/28**, including all eight
required mutants, the nine contract-branch pass-flag flips, detail suppression,
A6 unused helpers, the new A7 named-test flag+detail rule, and the base-control
forced-ruff comparison (141 = 141, zero new errors, none in the delivered file).

Goal v19 is goal v17 — the wording that produced the 26-test artifact — plus
exactly three corrections, each traceable to a measured failure:
1. the last-line regression test must also assert its detail (FAIL #3);
2. every verify_cycles step must carry the `action` key (A5, 18c);
3. `textwrap` must be called if imported (F401 in 18c).
No acceptance criterion relaxed, nothing removed.

Also established this round (contract-level, not goal wording):
- `LLM_TIMEOUT` must be >= 300s for whole-file deliveries in this repo. At 120s
  the single call that emits the ~350-line file times out while every other call
  in the same session returns in seconds.
- mistralai/mistral-large-4-1t became unusable mid-round (7 x 120s, then 300s
  timeouts with `llm_calls=0`), while the same payload returned in 8.8–9.1s
  earlier and poolside returns in 9–14s throughout. Recorded as an environment
  fact, not a defect in the delivery code.
- Two `delivery run` processes from 2026-11-12 (PIDs 32856/23984) were still
  alive ~22h later with live GitHub connections. Candidate defect for a future
  row: a run has no watchdog and no resume, so an abandoned launch lingers.

A FOURTH fresh independent verifier adjudicates this artifact. PR #86 (the
rejected v17 artifact) remains open and unmerged and must be closed if this
one supersedes it.

## Fourth verifier verdict: PASS (2026-11-13)

A fourth fresh independent verifier (own clone, own re-runs, no access to my
logs) returned **PASS** on `dlv-63f4ecea…`, bundle `54fe7b43…2af5`, goal v19.
Binding evidence it collected itself:

- patch sha256 = `14df5648…f35e` = record; `git apply` exit 0; reconstructed
  file sha256 = `c398123d…3481` = bundle `after_hash`; canonical `bundle_hash`
  recomputed from the delivery code's own normalization (`bundle.py:93-112`)
  = record. Main HEAD = contract base SHA; main `git status --short` = only
  `?? plans/`; patched clone = only the delivered file.
- `pytest tests/unit/test_eval_l0.py -q` exit 0, 26 passed; `ruff check .` exit
  0; forced `--isolated --select F401,F841,RUF100` identical to the pristine
  base control (141 = 141, zero new, zero removed, none in the delivered file);
  file-scoped forced ruff exit 0.
- All nine acceptance criteria PASS; criterion 8 flagged (437 lines vs soft
  ~220, contract says flag-only).
- The FAIL #3 defect is verified fixed: `test_files_append_only_fail_second_section`
  now asserts both `passed is False` and `'not append-only' in check.detail`;
  all 16 flag/detail-named tests assert BOTH; zero flag-only, zero detail-only.
- All eight required mutants CAUGHT (with per-test attribution, e.g. M1 caught
  solely by the last-line regression test via `-rf`), P2 detail suppression
  CAUGHT, all nine contract-branch flips CAUGHT, base restored to 26 passing
  between mutants.
- One surviving extra mutant, recorded out-of-scope as in FAIL #3: the
  `forbidden_paths_touched` bundle-null branch is outside the frozen coverage
  list.

Verdict chain for this row is now: FAIL (v1..v15 iterations) -> FAIL #2 (v15)
-> FAIL #3 (v17m, one flag-only test) -> **PASS (v19)**. Push approval moves to
the human gate.

## Push outcome (2026-11-13, approved by the operator)

The gate process from round 19 died with its terminal (session restart) and the
pipeline has no resume command. A re-entry script (`.scratch/repush-b2.py`)
replayed ONLY the approval + publish steps for the SAME frozen bundle: the
ChangeBundle was rebuilt from the record and its hash checked before anything
ran; the workspace was re-materialized (tarball at base_sha + git baseline),
the recorded patch applied with `core.autocrlf=false` (first attempt drifted to
CRLF and the publisher's hash guard correctly refused; after the fix
`workspace bundle match: True`); approval went through the same
TerminalPushApprovalPort, typed by the operator in the TTY; publish went through
the audited `GitHubChangeAdapter.publish`.

- Operator approval recorded in the gate: `shing26` / `APPROVE` for bundle
  `54fe7b43…2af5` (fourth-verifier PASS artifact, unchanged).
- PR #87: https://github.com/shing26/moa-gateway/pull/87, head 4d3f886a4ade5e5fa031941ea004c3e5b40da75c.
- Head blob sha256 of `tests/unit/test_eval_l0.py` verified via the GitHub API
  = `c398123d…3481` = bundle `after_hash` = the exact bytes the fourth verifier
  PASSed.
- CI: 1 passing / 0 failing / 1 skipped; mergeable CLEAN.
- Merge record registered: `shing26/moa-gateway#87`, state `watching`
  (`.scratch/register-merge-87.py`, same pattern as #82/#85).
- Run record state: `waiting_ci`.

Merge remains a human gate: written signoff in conversation is required before
MergeExecutor approves.
