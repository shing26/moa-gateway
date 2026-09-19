# ADR-008: LangGraph as an Optional Second Engine

**Status**: Accepted
**Date**: 2026-09-18

## Context

The request path is sequenced by a self-built FSM (`app/engine.py` +
`app/fsm/state_machine.py`) inside one imperative method,
`MoAPipeline.run()`. That is a deliberate choice — the transition table is
small, auditable, and dependency-free — but it left an untested question: would
a mainstream agent framework express the *same* path, and at what cost?

The honest answer cannot be a paragraph of prose, because prose cannot drift
with the code. It has to be an adapter that shares the collaborators and a test
that fails when the two runtimes stop agreeing.

## Decision

- `app/orchestration/graph.py` expresses the same sequence as a LangGraph
  `StateGraph` and reuses the *same objects*: router, retriever, prompt
  registry, evaluator, guard service, HITL store, adapter, conversation memory,
  long-term memory. It imports `_merge_guard` rather than re-implementing guard
  precedence, and every state it reports comes from `next_state()`.
- LangGraph stays an optional extra (`uv sync --extra langgraph`), not a
  default dependency. Resolving it pulls ~20 packages including langsmith. The
  Docker image syncs without extras.
- `ENGINE` (`fsm` default, `langgraph` optional) selects the orchestrator at
  startup. `app/orchestration/dispatch.py` routes per request; four paths stay
  on the FSM: slash-commands, `RESET`/`CANCEL`, `SENSITIVE_DETECTED`, and
  sessions with a pending approval or sensitive hold.
- Missing langgraph, an unknown `ENGINE` value, or a graph failure at runtime
  falls back to the FSM pipeline with a warning. Booting never depends on the
  optional extra.
- `SessionStore` is the cross-engine source of truth for pending approvals. The
  graph advances the FSM (`MESSAGE_RECEIVED`, then `NEEDS_HUMAN`) so the
  existing Feishu callback keeps working unchanged.
- The checkpoint is keyed by `trace_id`, not `session_id`. A thread left on
  `interrupt()` otherwise swallowed the session's *next* message, replaying it
  into the unfinished interrupt.

## Verification

- `tests/unit/test_langgraph_adapter.py` — field-by-field parity, HITL resume,
  and a drift guard that fails if `MoAPipeline`'s collaborator surface changes
  without a decision.
- `tests/unit/test_engine_parity_golden.py` — three deterministic scenarios
  (plain answer, real `TaskAgent` tool loop, review → approve/reject), plus
  long-term memory recall/write parity and the stuck-thread regression.
- `tests/unit/test_engine_dispatch.py`, `test_engine_selection.py` — the four
  fallback paths, single request-log write, and fail-safe selection.

## Consequences

The FSM remains the default and the only engine the product promises. The
second engine is evidence, not a replacement: it proves the standard framework
can implement the same constraints, and it keeps that claim honest by running
in CI. The cost is an optional dependency and one more runtime to keep in
parity; the drift guard is what keeps that cost bounded.
