# ADR 030 — LangGraph as a first-class *optional* execution backend

**Status:** Proposed
**Date:** 2026-05-27
**Deciders:** Engineering (orchestration/runtime) — **`langgraph` dependency
adoption requires Deva sign-off per ADR 001 / ADR 017 D4**
**Context window:** v1.x orchestration depth — support complex agent graphs
(cyclic/ReAct, parallel/map, supervisor) by promoting LangGraph from a codegen
scaffold to a real, optional execution backend.
**Builds on / related:** ADR 017 (extend native engine; adapt — don't adopt),
ADR 001 (cloud-portability + minimal-deps), ADR 008 (workflow evals), ADR 024
(per-step spans), ADR 029 (workflow authoring), and
`core/workflow/{spec,ir,compiler,runner,compilers/langgraph}.py`.

## Context

Today `compile_langgraph()` is a **code-generation scaffold**: it lowers the
workflow IR to a LangGraph `StateGraph` **source string** for preview/export
(`mdk export langgraph`), handles **linear graphs only**, and `langgraph` is not
a dependency. But the **IR already models the hard constructs** —
`EdgeKind.CONDITIONAL`, `PARALLEL_FAN_OUT`, `PARALLEL_FAN_IN` (tagged "v1.1"),
plus cycle detection. The native `WorkflowRunner` covers agent / intent-router /
human nodes over a DAG. What it does *not* offer — and what teams reach for in
genuinely complex use-cases — is **cycles/loops** (ReAct, reflection,
retry-until-grounded), **parallel fan-out/fan-in** (map over N items, concurrent
tools), and rich **conditional branching**. LangGraph is the mature engine for
exactly these.

## Decision

Promote LangGraph to a **first-class OPTIONAL execution backend** behind the
existing runner/compiler seam. The **native runner stays the default and the
portable floor**; LangGraph is selected explicitly and ships as an opt-in extra.
This keeps ADR 017's "adapt — don't adopt": LangGraph is a *swappable backend*,
never *the* engine, and never a core dependency.

### D1 — `LangGraphBackend` behind the runner seam
Add a backend that executes a compiled `StateGraph` in-process, selected via
`workflow.yaml: runtime: langgraph` (or `mdk run/dev --runtime langgraph`);
default remains the native runner. `langgraph` ships as the **opt-in extra
`mdk[langgraph]`** — base install, all existing commands, and the native runner
are unaffected (lazy import; assert the import-isolation contract).
**Dependency adoption is gated on Deva sign-off (ADR 001 / ADR 017 D4).**

### D2 — Grow the compiler to the IR it already models
Cover the constructs the IR defines but the compiler doesn't yet emit:
- **Conditional edges** → `add_conditional_edges` driven by `EdgeSpec.condition`.
- **Parallel fan-out / fan-in** → concurrent siblings + merge node.
- **Cycles / loops** → with a **mandatory max-iteration guard** (a recursion
  limit) so a loop can never run away (failure-mode rule).

### D3 — Typed state schema
Generate a typed LangGraph state from the workflow `state_schema` (per-node
contracts) instead of today's generic `dict[str, Any]`, so state is validated
and inspectable.

### D4 — Checkpointer over `StorageProvider`
Implement LangGraph's checkpointer protocol **backed by mdk's `StorageProvider`**
(the architectural decision ADR 017/the compiler explicitly deferred), so
durable + HITL state lives in *our* store — consistent with the native HUMAN
pause/resume node, and portable across the storage backends mdk already
supports. No LangGraph-specific persistence lock-in.

### D5 — Observability parity
Wrap node execution so ADR 024 spans + per-step cost + Langfuse/OTel tracing
flow through the LangGraph backend **identically** to the native runner —
tracing wired at the edges, never imported into execution logic (boundary rule).
A run on either backend produces the same trace shape.

### D6 — The complex patterns this unlocks
Document + template (via ADR 028's `workflow_init` and ADR 029's authoring
actions): **ReAct / reflection loop** (cycle + conditional exit), **parallel
map-reduce** (fan-out → per-item agent → fan-in), and **supervisor / multi-agent
handoff** (conditional routing among agent nodes). These become first-class,
testable workflow shapes — the payoff of the backend.

## Consequences

**Positive**
- Complex graphs (cyclic, parallel, supervisor) become buildable + runnable +
  observable, using LangGraph's mature engine — without rebuilding it.
- Native runner stays the portable default; LangGraph is opt-in, isolated behind
  the seam; cloud-portability (ADR 001) preserved.
- Checkpointing finally resolved, on *our* storage — durable/HITL works on
  either backend.

**Negative / risks**
- **New (optional) dependency** — version churn; pin + test against a known
  LangGraph version; Deva sign-off gate. Keep the backend thin.
- **Two backends to keep behaviorally consistent** — mitigate with a shared
  **conformance test suite** both backends must pass.
- **Cycle runaway** — mandatory max-iteration/recursion guard (D2).
- **Checkpointer correctness** — durable resume + concurrency; the largest
  correctness surface; design + test first.

## Boundaries
LangGraph is a backend behind the runner/compiler seam; `core` depends on the
seam, not on LangGraph; tracing at the edges; opt-in extra only. Adapt — don't
adopt (ADR 017) is preserved.

## Alternatives considered
- **Keep codegen-export only.** Rejected for this ADR (the user chose the full
  backend); export remains available as the no-dep path for users who run
  LangGraph themselves.
- **Adopt LangGraph as THE engine.** Rejected — violates ADR 017 portability +
  minimal-deps; the native runner must remain the floor.
- **Temporal instead.** Different problem (durable cross-system execution, ADR
  017 D4); orthogonal and still deferred.

## Scope / rollout
Multi-PR, **dependency adoption gated on Deva sign-off**: (1) grow the compiler
(conditional/parallel/cycles + typed state) — no runtime dep, ships value via
`mdk export`; (2) `LangGraphBackend` + the `mdk[langgraph]` extra +
import-isolation; (3) the `StorageProvider` checkpointer; (4) observability
parity + the conformance suite; (5) the complex-pattern templates/docs (with ADR
028/029). Sequence alongside ADR 029 (authoring + backend co-evolve).
