# ADR 092 — DAG workflows: parallel fan-out/fan-in + bounded supervisor

Status: Accepted — phased; Phase 1 building now.
Date: 2026-06-08
Deciders: Engineering + Deva (Movate)
Builds on: ADR 038 (governable pattern library), ADR 054/055 (Temporal backend),
ADR 091 (Temporal default runtime).

## Context

The platform's pattern story (ADR 038) is "governed primitives composed over the
one `WorkflowSpec` graph." Two of the patterns enterprises ask for first — and
that Deva called out — are **fan-out/parallel** and **managerial (supervisor)
delegation**. Both are, today, **not real**:

- `NodeType.PARALLEL_FAN_OUT` / `PARALLEL_FAN_IN` and the corresponding edges are
  **IR enum values only**. The shipped semantic gate, `validate_linear`
  ([compiler.py]), explicitly **rejects** "branches, joins, conditional edges,
  parallel fan-out/in" — "v0.3 = linear chain of agent nodes only." So a fan-out
  workflow **can't even be authored**: it fails validation.
- The native `WorkflowRunner` is a strict **single-successor walker**
  (`while current_id is not None: … _sequential_successor(...)`), so it has no
  way to run concurrent branches even if validation allowed them.
- The Temporal compiler has a latent `emit_fan_out` (→ `asyncio.gather`) but it
  is **not reached** by the node dispatch.
- There is **no `SUPERVISOR` node** at all — managerial delegation isn't a
  primitive in the spec/IR.

So the gap is in the **primitives + execution model**, not in needing a new
pattern format. (We are NOT adding a high-level `pattern:` DSL — ADR 038's
governable filter: patterns stay compositions of governed primitives over the
graph; "pattern" is an authoring-time/template concept.)

## Decision

Promote the workflow engine from **linear-only** to a **governed DAG**, adding
the two missing primitives behind a uniform governance contract. Phased so each
phase is independently shippable + reviewable, and the **native linear path stays
byte-for-byte unchanged**.

### D1 — `validate_dag` replaces the linear gate for non-linear graphs

A new `validate_dag` accepts fan-out/fan-in DAGs: a fan-out node has N>1
successors; a fan-in node has N>1 predecessors and joins. It rejects cycles
(outside the existing judge/router back-edges), dangling joins, and type
mismatches. `validate_linear` stays the gate for graphs that *are* linear (no new
risk for existing workflows). A graph is routed to the DAG validator only when it
declares a fan-out/fan-in edge.

### D2 — Native runner gains a fan-out/fan-in *block* executor

The walker keeps its single-successor advance for linear stretches; when it
reaches a fan-out node it runs the N branches concurrently
(`asyncio.gather`), each branch a normal sub-walk, then joins at the matching
fan-in node (merging branch state by a declared strategy: last-wins / by-key /
collect-into-list). This is the **canonical diamond** (one fan-out → N branches →
one fan-in) first; arbitrary nested DAGs are a later phase. The linear path is
untouched (the block executor is only entered at a fan-out node).

### D3 — Wire the Temporal `emit_fan_out`; cross-backend parity

Reach the existing `emit_fan_out` from the compiler's edge emission so a fan-out
block compiles to `await asyncio.gather(*[execute_activity(...) …])` — Temporal's
native parallelism. A conformance test asserts the native and Temporal backends
produce the same joined state for the same fan-out spec (the ADR 055 D7 pattern).
With ADR 091, a fan-out workflow's `runtime: auto` then prefers Temporal (where
parallel orchestration is durable) and falls back to native.

### D4 — Bounded `SUPERVISOR` node (the managerial pattern)

A new node type: a manager that delegates to a **specialist allowlist** with
**max-depth + per-delegation cost/token budget + canonical state**. The bounds
are the point (vs AutoGen/swarm entropy, ADR 038 D5). It compiles to a bounded
delegation loop on both backends. Declined: unbounded recursive spawning.

### D5 — Uniform governance contract on parallel/delegation (the moat)

Fan-out and supervisor carry declarative caps: `max_fanout`, per-branch
`budget`/`timeout`, `max_depth` (supervisor). Enforced at compile + runtime;
surfaced in `mdk validate`. This is what makes these patterns enterprise-grade.

### D6 — Backend-aware validation

`mdk validate` / `mdk workflow lint` warns when a declared pattern can't run on
the *resolved* backend (e.g. a fan-out on `runtime: native` before D2 lands, or a
`FUNCTION` node on Temporal) — so authors learn at author time, not runtime.

## Phasing (each a separate PR)

- **Phase 1 — native fan-out (the diamond):** `validate_dag` + the block
  executor (D2) + spec support for fan-out/fan-in edges + D6 lint + tests.
  *Outcome: a fan-out workflow authors, validates, and RUNS on native.* ← ready.
- **Phase 2 — Temporal fan-out + parity:** wire `emit_fan_out` (D3) + the
  cross-backend conformance test.
- **Phase 3 — bounded supervisor (D4):** the managerial primitive, both backends.
- **Phase 4 — governance-contract unification (D5):** uniform caps + enforcement.

## Consequences

**Compat / blast radius (rule 5):** Phase 1 is additive — a workflow with no
fan-out/fan-in edge takes the unchanged linear path (same validator, same
single-successor walk). Fan-out is a new, opt-in graph shape. No `/api/v1`,
CLI-flag, env, or storage change. The `max_fanout` cap (default small) prevents a
runaway. The change is to `core/workflow` (validator + runner + compiler) — the
engine internals, behind the existing `WorkflowSpec`/`WorkflowRunner` seams.

**Why not a `pattern:` DSL:** a high-level pattern field would fragment into N
special-cased runtimes (the ADR 038 trap). Keeping the runtime on governed
primitives + scaffolding patterns via templates/copilot is the durable path.

## Verification (per phase)

```
ruff check src tests && ruff format --check src tests && mypy src
pytest -m "not smoke" tests/test_workflow*.py tests/test_temporal_*.py
pytest -m "not smoke"            # linear path unchanged
```
- Phase 1: a diamond fan-out runs on native; join strategies; `max_fanout` cap;
  the lint warns on fan-out-under-explicit-native; linear workflows unchanged.
