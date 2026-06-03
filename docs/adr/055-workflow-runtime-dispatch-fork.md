# ADR 055 — Workflow runtime selection: one dispatch fork behind the runner seam (native · LangGraph · Temporal)

**Status:** Proposed
**Date:** 2026-05-30
**Deciders:** Engineering (orchestration/runtime) — **the `langgraph` /
`temporalio` dependency adoptions this fork selects into remain gated on Deva
sign-off per CLAUDE.md §8 / ADR 001 / ADR 017 D4. This ADR adds no new
dependency itself.**
**Context window:** the three workflow backends now exist as compilers but
none is actually selectable at runtime — close that gap with a single,
explicit selection seam.
**Builds on / composes with (changes nothing in any of them):**
ADR 017 (agent orchestration — the native `WorkflowRunner`, node types, the
runner/compiler seam),
ADR 030 (LangGraph as a first-class *optional* execution backend — **D1 says
"selected via `workflow.yaml: runtime: langgraph`" but the selection seam was
never built**),
ADR 054 (Temporal as an opt-in deterministic backend — **D2 says the runner
Protocol "dispatches the spec to this compiler" on `workflow.yaml: runtime:
temporal`, and Track B shipped the `CompilerProtocol` + compiler, Track C the
activity wrappers — but, again, nothing reads a `runtime` field yet**),
ADR 001 (cloud-portability + minimal-deps — the native runner stays the
portable floor),
ADR 018 (per-tenant BYOK / Key Vault — the Temporal connection rides the same
credential seam),
ADR 024 (per-step spans — observability parity across backends).

**Defining fact this ADR confronts.** As of `2026.5.30.x` the repository
contains *three* workflow execution paths — the native `WorkflowRunner`
(`core/workflow/runner.py`), the LangGraph compiler
(`core/workflow/compilers/langgraph.py`, ADR 030), and the Temporal compiler
(`core/workflow/compilers/temporal.py`, ADR 054, Track B) plus its activity
wrappers (`core/workflow/temporal_activities.py`, Track C) — but **there is no
`runtime` field on `WorkflowSpec` and no code anywhere selects a non-native
backend.** Every workflow runs on the native runner. ADR 030 and ADR 054 each
*assumed* the selection seam as if it existed; it does not. This ADR is the
single decision that makes the seam real, so the two backends those ADRs
designed become reachable without each re-inventing selection, override,
worker, credential, and fallback semantics differently.

This is a **design** ADR. It adds **zero** new dependency and **zero** new
default-path behavior: the field defaults to `native`, and a `native`
workflow takes byte-for-byte the same path it does today. The field, the
dispatch fork, the `--runtime` override, the `mdk worker --backend temporal`
command, and the connection-BYOK wiring land in follow-up PRs (see Scope).

---

## Context

Three forces make "we'll wire selection later" no longer tenable:

1. **Two backends are built but dead.** The LangGraph compiler (ADR 030) and
   the Temporal compiler + activities (ADR 054 Tracks B/C) are merged, tested,
   and import-isolated — yet unreachable. A capability that cannot be selected
   is, operationally, not shipped. The Monday-demo "we have a deterministic,
   durable backend" claim is only true once a `workflow.yaml` can ask for it.
2. **Selection was specified twice, inconsistently.** ADR 030 D1 says
   `runtime: langgraph` (and `mdk run/dev --runtime langgraph`); ADR 054 D2
   says the runner Protocol routes on `runtime: temporal`. If each backend
   wires its own selection, we get two half-overlapping forks, two override
   flags with different precedence, and two failure messages when an extra is
   missing. One seam, decided once, keeps the three backends true *peers*
   behind one contract (the `CompilerProtocol` already declared in
   `compilers/temporal.py`).
3. **The non-native backends carry operational surface the native one
   doesn't** — an opt-in extra that may be absent, a Temporal connection that
   may be unconfigured, a separate worker that hosts Temporal workflows. Those
   need *uniform* "fail loud at submit time with an actionable hint" semantics,
   not per-backend ad-hoc handling. That is a cross-cutting decision, i.e. an
   ADR.

The native `WorkflowRunner` remains the default and the portable floor
(ADR 001 / ADR 017). This ADR does not change it; it routes *around* it only
when a workflow explicitly opts in.

## Decision

Introduce **one explicit runtime-selection seam**: a `runtime` field on the
workflow spec, read by a **single dispatch fork** that hands the workflow to
the native runner or to a compiled backend. The three backends are peers
behind the existing seam; the native path is unchanged and always available.

### D1 — `runtime` field on `WorkflowSpec` (additive, default `native`)

Add an optional `runtime` field to the `workflow.yaml` contract
(`core/workflow/spec.py`):

```yaml
# workflow.yaml
runtime: native        # default — the in-process WorkflowRunner (today's behavior)
#        | langgraph   # ADR 030 backend (opt-in mdk[langgraph])
#        | temporal    # ADR 054 backend (opt-in mdk[temporal])
```

Typed as an enum, **default `native`**. This is **backward-compatible**:
`WorkflowSpec` sets `extra="forbid"`, so no existing `workflow.yaml` carries a
`runtime:` key today (it would have failed validation) — the field is purely
additive and every existing workflow keeps its exact current behavior. The
field is also surfaced read-only on the IR/`WorkflowGraph` metadata so the
dispatch fork and `mdk show` can see it without re-parsing.

> Compat note (flagged per CLAUDE.md rule 5): this is an additive change to the
> `workflow.yaml`/spec schema. No removal, no rename, default preserves
> behavior.

### D2 — One dispatch fork behind the runner seam

A single selection point — in the workflow dispatch path
(`runtime/dispatch.py` where the `WorkflowRunner` is constructed) and the
local run path (`cli/run.py`) — reads the **effective runtime** (D3) and:

- `native` → today's `WorkflowRunner._walk(...)` over the IR. No compile step.
- `langgraph` / `temporal` → lower the IR via that backend's
  `CompilerProtocol.compile(graph)` (the contract already in
  `compilers/temporal.py`: `compile()` + `lint()`), then execute on that
  backend.

The native runner consumes the IR directly (no compile); the two optional
backends compile first. That asymmetry already exists — this ADR names it as
the seam's contract rather than letting each backend invent its own entry. The
**fork is the only place** that knows more than one backend exists; `core`
depends on the `CompilerProtocol`/runner seam, never on `temporalio` or
`langgraph` (the import-isolation contract from ADR 030/054 D7 is preserved —
the fork lazy-imports the chosen backend only when selected).

### D3 — `--runtime` override + precedence

`mdk run` and `mdk dev` gain a `--runtime {native,langgraph,temporal}` flag
(ADR 030 D1 already promised it for langgraph; this generalizes it). Precedence,
highest first:

1. explicit `--runtime` CLI flag (per-invocation, for A/B + testing),
2. the `workflow.yaml: runtime` field (the workflow's declared home),
3. `native` (the default).

The override is **read-only selection** — it never mutates the spec. This makes
"run the same workflow on native vs temporal and diff the result" a one-flag
operation, which is also how the conformance suite (D7) drives all three
backends.

> Compat note: `--runtime` is a new optional CLI flag (additive). No existing
> flag changes. The runtime API surface (`/api/v1`) gains an optional
> `runtime` selector on the workflow-run submit body, default `native`,
> mirroring the field — additive, flagged per rule 5.

### D4 — `mdk worker --backend temporal`

Temporal workflows are hosted by a Temporal **worker** that registers the
compiled workflow + the Track-C activities. Add a `--backend` selector to the
worker command:

- `mdk worker` (default `--backend queue`) → today's job-queue worker
  (`runtime/dispatch.py`), which serves the native and langgraph (in-process)
  paths exactly as now.
- `mdk worker --backend temporal` → a Temporal worker that connects to the
  Temporal Service (D5), registers the generated `@workflow.defn` classes, and
  calls `temporal_activities.configure_activities(...)` at startup to install
  the `ActivityContext` (storage/pricing/tracer/provider/tenant) — the same
  Executor wiring `dispatch.py` builds, so Temporal activities reuse the one
  execution model (ADR 054 D3).

This is a **deployment-lifecycle surface change** (a new worker mode) — which
is precisely why it belongs in an ADR (CLAUDE.md rule 2). The queue worker is
untouched; the Temporal worker is additive and opt-in.

### D5 — Temporal connection via BYOK (ADR 054 D8), read only when selected

The Temporal connection rides the **same credential seam as every provider
key** (ADR 018): `mdk auth login temporal` captures `TEMPORAL_HOST`,
`TEMPORAL_NAMESPACE`, optional `TEMPORAL_TLS_CERT`, autoloaded from
`~/.movate/credentials` / per-tenant Key Vault. The dispatch fork reads these
**only** when the effective runtime is `temporal`; a `native`/`langgraph`
workflow never touches Temporal credentials. No new credential model — this is
the ADR 054 D8 decision, wired through the selection seam so it is read at
exactly one place.

### D6 — Availability + fallback semantics (fail loud, fail early)

The optional backends can be unavailable in two ways the native runner cannot:
the extra is not installed, or (Temporal) the connection is unconfigured.
Uniform rule:

- **At submit/selection time** (not at worker-launch, not mid-run): if the
  effective runtime is `langgraph`/`temporal` and its extra is not importable,
  or (temporal) no connection is configured, **reject with an actionable hint**
  (`The [temporal] extra is not installed. Install with: uv tool install
  --editable '.[temporal]' --force` — the message `temporal_activities.
  _require_temporalio()` and the compiler already raise).
- **Never silently downgrade** to native. A workflow that asked for
  determinism must not get best-effort in-process execution without the
  operator knowing — silent fallback would violate the very guarantee the
  selection expresses (failure-mode rule, CLAUDE.md rule 10).
- `native` is always available, has no extra, needs no connection — the floor
  never fails this check.

### D7 — Cross-backend conformance is the precondition for selection

Selection is only *safe* if the three backends are behaviorally equivalent on
the shared feature subset. Adopt the **conformance suite** ADR 030 named as its
key risk-mitigation: one parametrized suite that runs the same fixture
workflows on `native`, `langgraph`, and `temporal` and asserts identical
terminal `status`, final state, and per-node `RunRecord`s (modulo
backend-specific metadata). A backend may only be offered in the fork once it
passes. This turns "which runtime?" from a gamble into a declared trade-off
(determinism/durability vs. zero-dep portability) with the *same* result.

## Consequences

**Positive**
- The two already-built backends (ADR 030, ADR 054) become reachable — the
  capability is finally *shipped*, not just merged.
- One seam, one override precedence, one failure story, one credential read —
  instead of two backends each re-deriving them inconsistently.
- The native runner stays the default and portable floor; nothing in the base
  install or default path changes (field defaults to `native`; fork is a no-op
  for it).
- `--runtime` makes A/B comparison and the conformance suite trivial.

**Negative / risks**
- **Behavioral drift between backends** is the central risk — mitigated by D7's
  mandatory conformance suite as the gate to offering a backend.
- **A new worker mode** (`--backend temporal`) is operational surface to
  document + run (deferred runbook; ADR 054 Phase 2/3 deploy modules).
- **Selection-time availability checks** must be correct and early — a late or
  missing check is the difference between "clear install hint" and "obscure
  crash mid-run" (D6).
- The optional **dependency adoptions remain gated on Deva sign-off** — this
  ADR adds none, but it is the seam that makes them load-bearing, so the gate
  stays explicit.

## Boundaries

The dispatch fork is **control-plane selection**; the backends are
**execution-plane**. `core` depends on the `CompilerProtocol` / runner seam,
never on `temporalio`/`langgraph` (lazy import only inside the selected
branch; the ADR 030/054 D7 import-isolation contract is preserved and
re-asserted by test). Tracing stays wired at the edges (ADR 024), identical
across backends (D7). The `runtime` field is additive with a behavior-
preserving default; the `--runtime` flag and the API selector are additive and
optional; no storage-schema change. Native remains the floor (ADR 001 /
ADR 017 "adapt — don't adopt").

## Alternatives considered

- **Per-backend selection wired inside each backend's PR.** Rejected — this is
  literally the status quo that produced two inconsistent, unbuilt selection
  specs (ADR 030 D1 vs ADR 054 D2). Selection is one cross-cutting concern; it
  gets one decision.
- **Auto-detect the backend** (e.g. infer `temporal` if a cycle/HITL is
  present). Rejected — implicit execution-semantics switching is surprising and
  unportable; the workflow should *declare* its runtime, and an operator should
  be able to override it explicitly.
- **CLI flag only, no spec field.** Rejected — a workflow's runtime is a
  property of the workflow (it may *require* determinism), so it belongs in
  `workflow.yaml`; the flag is an override, not the source of truth.
- **A single unified runtime that abstracts all three.** Rejected — that is
  re-adopting a framework as *the* engine, violating ADR 017 portability /
  minimal-deps; the native runner must stay the floor and the others stay
  swappable peers.

## Scope / rollout

Multi-PR; this ADR is doc-only. Dependency adoptions (langgraph/temporal)
remain **gated on Deva sign-off**.

1. **The field + the no-op fork** — `runtime` on `WorkflowSpec` (default
   `native`) + the dispatch fork that, for `native`, calls today's runner
   unchanged; `langgraph`/`temporal` reach D6's "not yet wired / extra missing"
   guard. Ships the schema + selection skeleton with zero behavior change and
   zero new dep. Includes the import-isolation test.
2. **Wire the Temporal branch** — compile via Track B (`compilers/temporal.py`)
   + execute via Track C (`temporal_activities.py`) + `mdk worker --backend
   temporal` (D4) + connection BYOK (D5) + local `temporal server start-dev`
   smoke. (Composes with ADR 054 Phase 1.)
3. **Wire the LangGraph branch** — selection + execution for the ADR 030
   backend (co-sequenced with ADR 030's own rollout).
4. **The conformance suite (D7)** — the gate; a backend is only offered in the
   fork once it passes on the shared fixture set.

The `--runtime` override (D3) lands with step 1 (native-only choices) and gains
each backend as steps 2–3 land.
