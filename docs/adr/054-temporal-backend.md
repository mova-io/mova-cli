# ADR 054 — Temporal as a deterministic, durable workflow backend (behind the runner Protocol, opt-in)

**Status:** Proposed
**Date:** 2026-05-30
**Deciders:** Engineering (orchestration/runtime) — **`temporalio` dependency
adoption requires Deva sign-off per CLAUDE.md §8 / ADR 001 / ADR 017 D4**
**Builds on / composes with (changes nothing in any of them):**
ADR 017 (agent orchestration — the workflow engine + node types
SUPERVISOR/GATE/JUDGE/HUMAN, bounded fan-out, eval-gates, the full trace;
**D4 deferred a single external engine like Temporal for the case where a
deployment genuinely needs durable cross-system execution** — this ADR
satisfies D4),
ADR 030 (LangGraph as a first-class *optional* execution backend — **the
precedent**: an opt-in execution-backend swap behind the existing runner /
compiler seam, default stays the native in-process runner; this ADR
**generalizes that pattern to a second backend** and the three runners are
peers behind one Protocol),
ADR 042 (Bundle Composer — workflow specs live in bundles; the
`runtime: temporal` field is read off the same `workflow.yaml`),
ADR 045 D10 (Stateful sessions — conversation state already separated into
the session store; this ADR enforces that separation by keeping
**conversation state out of Temporal history**),
ADR 036 (per-tenant usage metering — meter at the **activity** boundary so
LLM-call and skill-call cost stays accurate under Temporal's automatic
retries),
ADR 018 (per-tenant BYOK / Key Vault — the Temporal connection
string/namespace/cert ride the same BYOK seam as LLM keys).

**Defining architectural fact.** Temporal's core promise is **deterministic
execution**: workflow code is event-sourced, every decision is recorded in
history, and **any worker can replay a workflow against that history and
arrive at the same state, byte-for-byte**. That is the property mdk does not
have today and cannot retrofit cheaply on the native runner — and it is the
property Deva called out as load-bearing for incident-resolution / multi-day
HITL / auditable enterprise workflows. This ADR adopts Temporal **as a backend
behind the existing runner Protocol** (`src/movate/core/workflow/runner.py`,
the same seam ADR 030 promoted LangGraph through) so customers who need
determinism get it without imposing it — or its dependency — on the rest.

This ADR adds **zero** new execution-plane behavior to the default install.
It is a **design** ADR; the compiler, activity wrappers, extra, and worker
command land in a follow-up PR (Boundaries).

---

## Context

Three live, customer-driven forces converge on the same gap:

1. **Determinism + replay.** Today a `WorkflowRunner` run is an in-process
   `_walk()` over the IR. A node failure, a runtime restart, or a "what
   actually happened on that 3-day incident-resolution run?" question hits
   the same wall: no event-sourced history, no replay, no guarantee that
   re-running with the same inputs produces the same trajectory. Temporal
   was built to solve exactly this: every workflow decision is appended to a
   history; given that history any worker reconstructs identical state.
2. **Durable long-running + HITL.** ADR 017 D5 shipped a HUMAN
   pause/resume node on the native runner (paused state persisted via
   `StorageProvider.save_workflow_run`, resumed by re-walking from the
   gate's sequential successor). That works for hours-to-day pauses; it
   strains at multi-week pauses with restarts of the runtime in between.
   Temporal's `wait_condition` + `signal` are natively durable for days /
   weeks / months and survive worker restarts as a matter of design.
3. **Operational separation.** ADR 017 D2 already separates the API and the
   KEDA-autoscaled worker; ADR 030 added LangGraph as a peer execution
   backend. Customers asking "can a fleet of workers replay durably with
   automatic retry/idempotency?" want the *next* peer — a backend whose
   operational model is workflows + activities + workers — without making
   the native runner go away.

ADR 017 D4 explicitly **deferred** Temporal as an execution backend, gated on
"only if a deployment genuinely needs it." Multiple recent deals do. This ADR
makes the gate concrete and ships the design for the next opt-in peer behind
the same Protocol.

---

## Decision

Adopt **Temporal as a first-class OPTIONAL execution backend** behind the
existing runner / compiler seam, peer to the native runner (default) and the
ADR 030 LangGraph backend. Selection is **per-workflow** in `workflow.yaml`
(`runtime: native | langgraph | temporal`, default `native`). The dependency
ships as the **opt-in extra `mdk[temporal]`**. Activities wrap — they do not
replace — the existing `Executor` and `SkillBackend`, so tracing, metering,
sessions, and BYOK flow through unchanged. **Determinism is the headline
property** Temporal buys; every decision below exists to preserve it.

### D1 — Temporal is a BACKEND behind the runner Protocol, not the engine

Add a `TemporalBackend` that implements the same runner contract as the
native `WorkflowRunner` (`src/movate/core/workflow/runner.py`) and the ADR 030
`LangGraphBackend`. The native runner stays the **default and the portable
floor**: zero new deps, fast local iteration, the `mdk run` / `mdk dev`
inner loop unaffected. Temporal is selected **explicitly**. This is the
**same pattern ADR 030 D1 established for LangGraph**, generalized to a
second backend — the three runners are peers behind one seam. `core`
continues to depend on the seam, never on `temporalio` or `langgraph`. The
boundary rule (CLAUDE.md §6) is preserved.

### D2 — Per-workflow opt-in via `workflow.yaml: runtime:`

The runtime is chosen per workflow, not per project, so a single project can
mix backends:

```yaml
# workflow.yaml
runtime: temporal      # native (default) | langgraph | temporal
```

This is the same field ADR 030 D1 added for `langgraph`; this ADR widens its
enum by one value (additive — see "New surfaces" below). A typical project
might keep its conversational chatbot on `native` (cheap, in-process), flip
its multi-day incident-resolution workflow to `temporal` (durable, replay),
and use `langgraph` for a ReAct-heavy reflection loop — **all in the same
project, all behind one Protocol**. Hybrid is explicitly supported.

### D3 — Activities reuse the existing Executor + SkillBackend (no second execution model)

Every mdk node, when compiled to Temporal, becomes a Temporal **activity**
whose body is `await executor.execute(bundle, request, ctx)` —
`src/movate/core/executor.py`, the same tool-use loop the native runner
calls. Skills dispatch through `SkillBackend.execute(...)` —
`src/movate/core/skill_backend/base.py`, unchanged. The Executor does not
know it is running under Temporal; the Temporal activity is a thin shim that
forwards the same arguments and propagates the same error taxonomy
(`SkillErrorType`, `RunResponse`).

This is non-negotiable: **there is no second execution model, no bypass
route, no Temporal-specific Executor**. Tracing (ADR 024 spans), metering
(ADR 036), session state (ADR 045 D10), BYOK (ADR 018), and cost/error
shapes (ADR 002) all flow through one place. Two execution models would mean
two places to fix every future Executor change; mdk has exactly one.

### D4 — Node-type mapping

| ADR 017 node type | Temporal compilation |
|---|---|
| Agent call | `workflow.execute_activity(call_agent, ..., retry_policy=...)` — automatic retries with exponential backoff |
| SUPERVISOR | A parent workflow that calls child workflows / activities for sub-agents (composition is a Temporal primitive) |
| GATE | An activity returning a routing decision; the workflow picks the next branch from the result |
| JUDGE | An activity returning the judge's verdict + score; the workflow gates on it |
| HUMAN (ADR 017 D5 HITL) | `workflow.wait_condition(...)` + an external `signal` — **durable for days / weeks**, survives worker restarts |
| Bounded loop / fan-out | `for i in range(max_iterations)` in workflow code; cycle-free + bounded (CLAUDE.md failure-mode rule) |
| Skill call | An activity dispatched through `SkillBackend` |
| KB / graph retrieval | An activity (network IO must live in activities — D5) |

Each row is a **deterministic** lowering: every non-determinism (clocks,
randomness, network IO) moves into an activity, where Temporal records the
result in history so replay reproduces it.

### D5 — Determinism enforcement at compile time

Temporal's replay guarantee requires workflow code to be **deterministic** —
no `time.time()`, no `random.random()`, no network IO, no environment reads
outside activities. The mdk Temporal compiler enforces the constraint **at
compile time**, not at runtime crash time:

- Any node primitive that needs a clock / RNG / IO is lowered into an
  activity, never into workflow body.
- The compiler walks the IR and rejects (with a clear error) any
  user-supplied workflow code path that would call into a non-deterministic
  primitive directly from workflow scope.
- This is exactly why Temporal can guarantee replay: the *only* sources of
  non-determinism are recorded activity results, which history pins.

Document the constraint loudly in the workflow author docs (Consequences,
below): authors write **decisions** in workflow scope and **side effects** in
activities. The compiler help-text points at this ADR.

### D6 — Workflow ID = mdk run_id

The Temporal workflow ID **is** the mdk `run_id` (specifically
`workflow_run_id` from `runner.py`). One identity stitches Temporal Web's
event view to `mdk runs show` / trace replay / ADR 024 spans — operators
asking "what happened on this run?" land in **one** answer, not two
separately-indexed records. There is one source of truth for run identity;
the Web UIs are two views of it.

### D7 — `mdk[temporal]` opt-in extra (`temporalio` SDK, MIT, ~50MB)

The Temporal Python SDK ships as the opt-in extra `mdk[temporal]`:

```toml
temporal = [
    "temporalio>=1.7,<2",   # MIT-licensed; the only Python SDK for Temporal
]
```

Per CLAUDE.md §8 (minimal dependencies, license gate):

- **Justified.** `temporalio` is the *only* Python SDK for Temporal — hand
  rolling JSON-over-gRPC against the Temporal Service API is not viable and
  would duplicate a heavily-tested upstream. The dependency is single-purpose
  and well-bounded.
- **Permissively licensed.** MIT — passes `scripts/check_licenses.py
  --strict`. Confirmed before the implementation PR.
- **Opt-in only.** Base install + every existing command + the native runner
  + the LangGraph backend are wholly unaffected; nothing in `core` imports
  `temporalio` at module scope. Import isolation is a contract (same as ADR
  030 D1) — the implementation PR ships the test.
- **Sized for review.** ~50MB installed; in line with the other heavy opt-in
  extras (`voice`, `cross-encoder`).

**Adoption gated on Deva sign-off** per CLAUDE.md §8 / ADR 001 / ADR 017 D4.

### D8 — Connection BYOK

Temporal connection details ride the **same BYOK seam as LLM keys** (ADR 018,
per-tenant Key Vault / `~/.movate/credentials` file). Customer chooses
**Temporal Cloud** (managed) or **self-hosted** (AKS, docker-compose, or
`temporal server start-dev` locally) and registers the connection via:

```
mdk auth login temporal
# prompts: host (e.g. <ns>.tmprl.cloud:7233), namespace, optional TLS cert
```

Connection vars (additive to `~/.movate/credentials` autoload):

- `TEMPORAL_HOST`
- `TEMPORAL_NAMESPACE`
- `TEMPORAL_TLS_CERT` (path)

Same pattern as every other provider credential — no new credential model.

### D9 — Per-activity timeouts + heartbeating

Long LLM calls (deep-research, reflection loops, multi-step agents) need
both upper bounds and liveness signals:

- **`schedule_to_close_timeout`** is set per activity (default 5min;
  override in `workflow.yaml`).
- **`heartbeat_timeout`** (default 30s) plus an activity-side heartbeat for
  long calls so Temporal does not retry a still-running activity as a
  spurious failure.
- Timeouts and retry policy are configurable per node in `workflow.yaml`
  (Phase 3 surface; safe defaults in Phase 1).

This is the standard Temporal pattern — documented here so the implementation
PR has a single source for the defaults.

### D10 — Sessions hold conversation state, Temporal holds CONTROL FLOW

**The critical separation.** mdk's session store (ADR 045 D10 — stateful
sessions, conversation history per session) holds **conversation state**:
prior turns, persisted context, the user-facing record. Temporal holds
**control flow**: which activity fired, which branch was taken, the routing
decisions, the activity results that *drive* the next step.

Why this matters:

- Temporal history has a practical size ceiling (~50MB per workflow,
  hard-recommended much lower). Stuffing conversation history into workflow
  state would hit the ceiling on real workloads.
- Conversation state belongs in a queryable, bounded, evictable store —
  exactly what the ADR 045 D10 session backend already is.
- The Executor already reads conversation state from the session (ADR 045
  D10); the Temporal activity reuses the Executor (D3), so the session
  remains the conversation's home **automatically**. No new wiring.

Activities pass the **session id** as an input and read/write conversation
state through the session API. Workflow scope only holds: the session id,
the current node id, activity result handles, branch decisions. Document
this rule prominently in the author docs (it is the one easy thing to get
wrong).

### D11 — Activity-level metering

ADR 036 metering wraps **the activity** (the LLM call, the skill call), not
the workflow:

- Cost per LLM call / skill call is accurate even under Temporal's automatic
  retries — the retried attempt is metered as a retried attempt, not as a
  duplicated first attempt, because the meter records the deterministic
  activity attempt id.
- Quota checks fire at the activity boundary, not on workflow scheduling, so
  the abuse / cost guardrails per ADR 036 stay correct under durable retry.

The implementation PR wires the existing ADR 036 meter through the activity
shim; no new meter is built.

### D12 — Operational surface

`mdk worker --backend temporal` launches a Temporal worker pool. It registers
against the configured namespace (D8), polls the workflow task queue,
executes activities through the Executor (D3). Workers scale **independently
of the API**:

```
API container (control plane, /api/v1)
            ⊥
Worker container (execution plane, runs Temporal workflows + activities)
```

This is the same control-plane / execution-plane boundary CLAUDE.md §6 and
ADR 017 D2 already separate; the Temporal worker is the next implementation
of the same shape. Scaling can be per-queue (priority lanes for
incident-resolution, batch lanes for evals) — Phase 3.

---

## Three-phase plan

| Phase | Scope | Effort |
|---|---|---|
| **Phase 1 (this ADR + the build)** | This ADR + `src/movate/core/workflow/compilers/temporal.py` (IR → workflow + activity scaffolding) + `mdk[temporal]` extra + activity wrappers around `Executor` / `SkillBackend` + local `temporal server start-dev` smoke + `mdk worker --backend temporal` + `workflow.yaml: runtime: temporal`. Native runner remains default. | M-L (~3 weeks build, post-ADR) |
| **Phase 2** | HITL signal/query for the HUMAN node (durable HITL — pause for days/weeks); Temporal Cloud connect via `mdk auth login temporal`; deploy Bicep module for self-hosted Temporal-on-AKS. | M |
| **Phase 3** | Per-activity retry / timeout policies in `workflow.yaml`; activity heartbeating for long LLM calls; workflow visibility integration (`mdk runs show` ↔ Temporal Web link, same `run_id` per D6); replay-from-history CLI (`mdk workflow replay <run_id>`). | M |

Each phase ships behind the same opt-in extra; the default install never
changes shape.

---

## Consequences

**Positive**

- **Determinism guarantee.** Workflows are bit-for-bit reproducible; replay
  debugging becomes possible — point a worker at a historical run_id and
  watch it reconstruct identical state. This is the headline win.
- **Durability.** Runtime restarts no longer lose in-flight workflows;
  multi-day / multi-week HITL flows become tractable, beyond the comfort
  zone of the native runner's pause/resume.
- **No bespoke retry / idempotency code.** Every activity gets automatic
  retries with exponential backoff and deterministic attempt ids — replacing
  retry boilerplate.
- **Auditable execution.** Temporal Web shows every workflow's full event
  timeline; combined with mdk tracing (ADR 024) and the shared `run_id`
  (D6), customers get a verifiable, queryable record of *what happened* on
  demand.
- **Scaling story improves.** Workers scale independently of the API; per-
  queue pools enable priority separation (Phase 3).
- **Boundaries preserved.** No core dep growth; `core` still depends on the
  Protocol, not on `temporalio`; tracing still wired at the edges.

**Negative / risks**

- **Operational footprint grows.** A Temporal Service (Temporal Cloud
  subscription or self-hosted cluster on AKS / docker) becomes a runtime
  dependency for workflows that opt in. Document the operator overhead
  honestly.
- **Determinism constraint is real friction.** Workflow authors can no
  longer call `time.time()` / `random.random()` / network IO in workflow
  scope — those move to activities. The compiler enforces it, but it is a
  mental-model shift; the docs must lead with it (D5).
- **Three backends to keep behaviorally consistent.** Native, LangGraph,
  Temporal. Mitigation: the **shared conformance test suite** ADR 030
  introduced grows to cover the third backend; every backend must pass it.
- **Workflow history size.** D10 mitigates this by keeping conversation
  state out of workflow history; the implementation PR adds a hard guard
  + a workflow-author warning when history grows beyond a threshold.
- **Dependency pin churn.** `temporalio` versions; pin tight + test against
  a known version (same posture as `langgraph` in ADR 030).

---

## Alternatives considered

- **Temporal as THE engine (replace the native runner).** Rejected. Forces a
  heavy dep on every user, breaks CLAUDE.md §8 minimal-deps, breaks the
  simple-local-dev story (`mdk run` no longer works without a Temporal
  Service), and breaks the ADR 030 pattern. The native runner must remain
  the portable floor.
- **Bespoke durability layer (Postgres-backed workflow state + manual retry
  / idempotency).** Rejected. Reinventing Temporal poorly, no determinism
  guarantee, no event-sourced history for replay, large ongoing maintenance
  burden — exactly the kind of "framework we'd build internally" CLAUDE.md
  §8 warns against.
- **AWS Step Functions.** Rejected. Cloud-locked (mdk is multi-cloud per
  CLAUDE.md §1 / ADR 001); a customer running on-prem or on GCP gets nothing.
  Step Functions also has weaker primitives for long-running HITL.
- **Cadence.** Rejected. Cadence is Temporal's *predecessor* — Temporal is
  the maintained fork. Adopting Cadence is adopting an older, less-supported
  version of the same engine.
- **Prefect (as the durable engine).** Rejected. Prefect is task-orchestration
  oriented and lacks deterministic replay against an event-sourced history
  — the single property D5 builds the whole ADR around.
- **Stay native-only, add ad-hoc retry / checkpointing.** Rejected. Leaves a
  real, demonstrated customer gap (durable multi-day HITL, replay for
  incident postmortems); accumulates retry / checkpoint tech debt that
  Temporal already solves; no determinism guarantee.

---

## Boundaries (out of scope)

- **Implementation.** This is the ADR. Phase 1 builds the
  `compilers/temporal.py` compiler, activity wrappers, the `[temporal]`
  extra, and `mdk worker --backend temporal` in a **follow-up PR**.
- **Temporal Cloud account setup / customer billing.** Operator-run, like
  Azure subscription setup.
- **Worker autoscaling policies.** Phase 3 (per-queue pools, KEDA against
  Temporal task-queue depth).
- **Workflow versioning / patching.** Temporal's `workflow.patched`
  mechanism for evolving long-running workflows is a real concern but is a
  future story; documented here so it does not surprise anyone.
- **LangGraph backend interaction.** ADR 030 and this ADR are **peers**
  behind the runner Protocol; the three runners (native, langgraph,
  temporal) share the seam, and a workflow picks one. They do not call into
  each other.

---

## New surfaces (CLAUDE.md §5 — flagged, all additive)

- `workflow.yaml: runtime: temporal` — **additive enum option** on the field
  ADR 030 D1 added (`native` | `langgraph` | `temporal`). Default unchanged
  (`native`). No existing workflow is affected.
- `mdk[temporal]` — **additive opt-in extra** in `pyproject.toml`. Default
  install unaffected. License-gated through `scripts/check_licenses.py
  --strict` before the implementation PR merges.
- `mdk auth login temporal` — **additive provider** under the existing auth
  CLI surface (ADR 018 BYOK pattern). No existing provider changes.
- `mdk worker --backend temporal` — **additive flag** on the existing
  `mdk worker` verb. Default backend unchanged.
- `~/.movate/credentials` autoload — **additive vars** `TEMPORAL_HOST`,
  `TEMPORAL_NAMESPACE`, `TEMPORAL_TLS_CERT`. No existing vars touched.

Every surface is purely additive. No deprecations. No `MOVATE_*` / `MDK_*`
env-var renames. No `/api/v1` shape changes. No storage-schema migrations.
Backward compatibility is preserved per CLAUDE.md §5.
