# Contract — pipeline-gates-v1: post-verify permanent gates

Binding: this file freezes scope before implementation. The goal below is
authoritative on conflicts.

## Background (measured, from dogfood ledger rows 12–13)

The repo's ruff config ignores F401/F841/RUF100, so `ruff check .` stays green
while delivered files carry dead code (18c: unused `textwrap` import). And
generated test files can contain assertion-free ("vacuous") tests. Both defect
classes are currently caught only by out-of-pipeline harnesses. This task makes
them permanent, in-pipeline, post-verify gates so EVERY governed change is
gated, not just the ones someone hand-rolls a harness for.

## Allowed paths

Exactly these; nothing else created or modified:

```
apps/code_review_pipeline/change/gates.py        (new)
apps/code_review_pipeline/change/pipeline.py     (call the gates in prepare_bundle)
apps/code_review_pipeline/change/__init__.py     (export GateReport only if needed)
tests/unit/test_change_gates.py                  (new)
```

## Verification commands

```
python -m pytest tests/unit/test_change_gates.py -q
python -m pytest tests/unit -q
python -m ruff check .
```

## Acceptance criteria

1. `gates.py` defines a frozen dataclass `GateReport` with at minimum:
   `name: str`, `status: Literal["passed", "failed", "skipped"]`,
   `violations: tuple[GateViolation, ...]`, `skipped_reason: str = ""`, and a
   `GateViolation` carrying `path`, `detail` (and line info when available).
2. `forced_lint_gate(workspace: Path, added: Sequence[str], modified: Sequence[str]) -> GateReport`
   runs `[sys.executable, "-m", "ruff", "check", "--isolated", "--select",
   "F401,F841,RUF100", --output-format, "json", <files>]` in the workspace.
   - ADDED files: any finding is a violation.
   - MODIFIED files use a BASE CONTROL: extract the HEAD version of each file
     (`git show HEAD:<path>`), run the same command on it, and compare per
     `(file, code)` finding COUNTS (not rows — rows shift on edit). A finding
     count greater than HEAD's for the same code is a violation; fewer or equal
     passes. This deliberately tolerates "moved" pre-existing findings.
   - If ruff is unavailable (`importlib.util.find_spec("ruff") is None`), return
     status `skipped` with a reason. An unexpected ruff exit code (not 0/1) is
     also `skipped` with the stderr excerpt as reason — the gate must never
     crash the pipeline, but every skip MUST land in the audit trail.
3. `test_strength_gate(added: Sequence[str], modified: Sequence[str], workspace: Path) -> GateReport`
   AST-parses each changed test file (path containing `tests/` or basename
   starting with `test_`). Every function whose name starts with `test_`
   (module level or inside a class) must contain at least one `ast.Assert` node
   or a `pytest.raises` / `self.assertRaises` / `unittest.raises`-style call.
   Violations are `(path, qualname)`. A file that fails to parse (SyntaxError)
   is itself a violation. Files with no test functions pass trivially.
4. `prepare_bundle` in `pipeline.py`: after verify success and non-empty
   bundle, collect changed paths from the bundle (added + modified, skipping
   deleted), partition into `.py` sources and test files, run both gates, and:
   - any `failed` gate returns
     `ChangeResult(status=ChangeStatus.GATE_FAILED, ...)` — add
     `GATE_FAILED = "gate_failed"` to `ChangeStatus` — with a message naming
     each violation as `path:detail`;
   - the returned bundle is still attached (evidence is kept);
   - `audit` gains `"gates": [<gate reports as dicts>, ...]` on EVERY path
     through the gate block, including skips and passes.
5. No `.py` files changed → both gates `skipped` with reason "no python files
   changed", status remains VERIFIED.
6. Unit tests in `tests/unit/test_change_gates.py` cover at minimum:
   - added file with an unused import → lint gate fails, violation names the
     file and F401;
   - modified file whose HEAD version already has an F401 and whose working
     version keeps exactly that one → gate passes (base control works);
   - modified file with a NEW F841 → gate fails;
   - ruff-missing skip path (monkeypatch `find_spec`) and its audited reason;
   - a test function with no assert and no raises-check → test gate fails with
     its qualname; a `pytest.raises`-only test passes;
   - `prepare_bundle` integration: build a workspace via the existing test
     fixtures pattern (git init + commit + modify) where the change adds a
     vacuous test file and assert `ChangeStatus.GATE_FAILED`; and one where the
     change is clean and status stays VERIFIED with gates in audit.
   Fixtures use tmp_path git repos — no network.
7. Full `tests/unit` stays green; `ruff check .` clean; no `# type: ignore`.

## Forbidden

- Modifying `app/cli.py`, `apps/code_review_pipeline/delivery/**` (other tasks
  own those), `eval/**`, or any existing test's assertions.
- Any `# noqa`/`# type: ignore` suppression in product code.
- Making the gates silently fail-open: every skip must carry a machine-readable
  reason that reaches the audit dict.
- Adding dependencies.
