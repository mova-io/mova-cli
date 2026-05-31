# ADR 065 — Temporal as mdk's optional durable-execution seam

**Status:** Proposed
**Date:** 2026-05-31
**Deciders:** Engineering (platform direction — human-ratified)
**Context window:** establish a *strategic, principled* path for deeper Temporal
use across mdk — **as an optional, seam-based, incremental upgrade where the
native in-process engine stays the default floor**, not a rewrite and never a
hard dependency. Captures the direction so each per-operation adoption is a thin
step against an agreed contract rather than ad-hoc.
**Builds on / composes with (changes nothing in any of them):**
ADR 054 (Temporal as the *workflow* backend — the compiler, activity wrappers,
worker, determinism linter, and the D10 "control flow in workflow scope, state
in the store" rule; this ADR generalizes the *seam idea* beyond workflows),
ADR 055 (the runner dispatch fork — native / LangGraph / Temporal as peers
behind one Protocol, **the precedent this ADR mirrors**), ADR 062 (durable
HITL — already a Temporal-shaped operation), ADR 063 (the fine-tune loop —
whose long-poll worker is the cleanest first new adoption), and the existing
async job system (`JobRecord` + `worker.py` + `dispatch.py` — the hand-rolled
durable-execution layer this ADR positions Temporal *behind a seam* to augment,
never to rip out).

**Defining observation.** mdk has, by necessity, hand-rolled pieces of a
durable-execution engine in several places — the `JobRecord` queue with its
reaper, visibility timeout, at-least-once delivery, retry/backoff, dead-letter
management, and idempotency keys; the `source: "llm"` SSE multi-stage pipeline;
the native HUMAN pause/resume (ADR 017 D5). These work. But they are *partial
reimplementations* of exactly what Temporal provides, and several genuinely
long-running mdk operations (a fine-tune that polls for hours, a multi-step
Azure deploy that must roll back on failure, a continuous-eval schedule, a
large KB crawl) strain the bespoke layer. Temporal's Replay 2026 release
(**Workflow Streams** for durable LLM token streaming, **Standalone Activities**
so a durable job needs no workflow ceremony, **External Payload Storage** for
large AI payloads, **Nexus** for cross-namespace composition, **Worker
Versioning**, **Task-Queue Fairness**) lands these capabilities precisely where
mdk needs them. The question this ADR answers is *how much to lean in, and how
to do it without betraying mdk's zero-infra-local floor.*

This is a **strategic / design** ADR (rule 1/2). It introduces no new runtime
behavior on its own — it establishes a Protocol seam, a non-negotiable
native-floor rule, a connection-config model, and a prioritized per-operation
adoption order. Each adoption ships in its own ADR/PR against this contract.

---

## Decision

### D1 — A `DurableExecution` seam, mirroring the runner Protocol

Introduce a `DurableExecution` Protocol (the durable analogue of the runner
Protocol, ADR 055) with two implementations:

- **`NativeDurableExecution`** — the *floor*. Wraps today's `JobRecord` queue +
  worker. **This is the default and the portable, zero-infra path.** Local
  `mdk serve`, every existing test, and any deployment without Temporal use it,
  byte-for-byte unchanged.
- **`TemporalDurableExecution`** — the *upgrade*. Backs durable operations with
  Temporal (workflows / standalone activities). Opt-in `[temporal]` extra; lit
  up by config on deployments that run a Temporal server.

`core` depends on the Protocol, never on `temporalio` (rule 6/7). The
already-built workflow backend (ADR 054) becomes one consumer of this seam, not
a special case.

### D2 — Native is the floor, forever (the non-negotiable)

Temporal is **never a hard dependency** and **never required to run mdk**. The
native impl must remain a complete, correct path for every durable operation.
This is the same contract ADR 054 D1 / ADR 055 made for the runner — and it is
what keeps mdk embeddable in a customer deliverable with no Temporal cluster.
A PR that makes any core path *require* Temporal is rejected by this ADR.

### D3 — Connect via config, so one mdk runs anywhere

A `MOVATE_TEMPORAL_*` block (`ADDRESS`, `NAMESPACE`, `TLS_CERT` / `TLS_KEY`,
`TASK_QUEUE`) selects + connects the Temporal impl. The **same** mdk binary runs
against `temporal server start-dev` (local), an ACA/AKS-self-hosted server, or
Temporal Cloud — no code change, just config. Absent the block → native impl.
This is the seam's selection mechanism (mirrors how storage selects on
`MOVATE_DB_URL`).

### D4 — Adopt per-operation, in leverage order — NOT a big-bang migration

The existing job queue is **not ripped out**. Operations move behind the seam
incrementally, each justified on its own:

1. **Net-new durable ops first (lowest risk).** The **fine-tune worker**
   (ADR 063) — a job that polls a provider for *hours* — is the textbook first
   adoption: on Temporal it is a durable `wait_condition`/timer, not a blocked
   worker slot. No migration, isolated feature.
2. **Durable HITL** (ADR 062) — already Temporal-shaped; completes its rollout
   on the seam.
3. **Back the async job system** (eval / bench / agent runs) with **Standalone
   Activities** — the biggest code-deletion win: the reaper, visibility timeout,
   retry/backoff, and dead-letter become Temporal's, behind the seam, with the
   native impl still the floor.
4. **Sagas & timed flows** — the Azure **deploy lifecycle** (build→push→bicep→
   roll→health-check→**rollback**) as a saga; **canary rollouts** (ADR 016) as a
   timed promote/rollback workflow; **continuous eval** (ADR 016) as a Temporal
   schedule.
5. **Streaming & scale** — **Workflow Streams** for durable `mdk run --stream` /
   playground token streaming; **External Payload Storage** (Azure Blob driver)
   for large KB/dataset/fine-tune payloads; **Nexus** for multi-agent
   composition; **Task-Queue Fairness** + **Worker Versioning** for the
   multi-tenant production story.

Each step is its own ADR/PR. mdk can stop at any rung — the native floor never
goes away.

### D5 — Carry ADR 054's discipline to every adoption

The rules that made the workflow backend safe apply to *every* operation moved
behind the seam: **state stays out of workflow scope** (D10 — Temporal history
is control flow, not a database; conversation/result state lives in the store);
the **determinism linter** gates anything compiled to a workflow (clocks/RNG/IO
into activities); **idempotency** is preserved (at-least-once is the contract on
both impls). These are not re-litigated per adoption — they are the seam's
invariants.

### D6 — Hosting is separable infra (and also seam-selected)

Standing up the Temporal *server* is an ops task, independent of this code. The
recommended in-tenant path is **Azure Container Apps + the existing Azure
Database for PostgreSQL** behind an `enableTemporal=true` bicep flag (default
off — same pattern as `enablePlayground` / `useAzureFiles`), with `start-dev`
for local/demo and Temporal Cloud as the zero-ops alternative. Because of D3,
the runtime is indifferent to which.

## Consequences

**Positive**
- One **durable-execution contract** for every long-running mdk op, instead of
  N bespoke reimplementations — and Temporal's correctness (retries, dead-letter,
  replay, observability) for free on the operations that adopt it.
- **Zero risk to the local floor**: native stays default; adoption is opt-in,
  per-operation, reversible.
- Strategic alignment with Temporal's AI-agent direction (Replay 2026) — mdk is
  positioned as a durable agent platform, and a candidate Temporal AI-ecosystem
  partner.

**Negative / risks**
- **Operational cost**: a Temporal cluster is real infra; justified per
  deployment, which is exactly why D2 (native floor) + D6 (opt-in hosting)
  matter. Over-adopting (requiring Temporal) would betray mdk's embeddability —
  bounded by D2.
- **Two code paths** per adopted operation (native + Temporal) — mitigated by
  the conformance-suite pattern (ADR 055 D7) already used for workflows: native
  == Temporal on the shared contract, asserted in tests.
- **Scope creep toward "everything is a workflow"** — bounded by D4's
  per-operation justification + the explicit "stop at any rung" stance.

## Boundaries

A new adapter seam (rule 7) parallel to `StorageProvider` / `Tracer` / the
runner Protocol. `core` depends on the Protocol, never `temporalio`. Native is
the floor (rule 6 — local-first, embeddable). Additive + opt-in + reversible.
Mirrors ADR 054/055 rather than inventing a new pattern.

## Alternatives considered

- **Go all-in: make Temporal the execution engine for everything.** Rejected —
  breaks the zero-infra local floor + embeddability (rule 6), forces a heavy
  dependency on every customer, and is a high-entropy rewrite of working code.
- **Stay fully bespoke; never deepen Temporal.** Rejected — leaves mdk
  reimplementing (imperfectly) what Temporal does correctly, and forgoes the
  Replay-2026 AI capabilities (durable streaming, standalone activities) right
  where mdk needs them.
- **Adopt Temporal Cloud only (no self-host path).** Rejected — an external SaaS
  dependency conflicts with the "in the customer's Azure tenant" deliverable
  model; D6 keeps self-host (ACA/AKS) as a first-class option, Cloud as one
  choice among several.
- **A bespoke new durable engine instead of Temporal.** Rejected — that is the
  thing we're trying to *stop* hand-rolling.

## Scope / rollout

Strategy ADR; no code lands here. Implementations are per-operation ADRs/PRs in
the D4 order, each conformance-tested (native == Temporal). The seam (D1) + the
connection config (D3) ship first, with the fine-tune worker (D4.1) as the
proving adoption.
