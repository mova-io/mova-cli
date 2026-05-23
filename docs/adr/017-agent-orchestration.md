# ADR 017 — Agent orchestration on Azure: extend the native engine, adapt (don't adopt) external orchestrators

**Status:** Proposed
**Date:** 2026-05-23
**Deciders:** Engineering (orchestration/runtime — Deva sign-off for any external-orchestrator dependency, per ADR 001)
**Context window:** v1.0 Azure operability — scheduled + multi-step agent orchestration
**Builds on / related:** ADR 001 (cloud-portability + minimal-deps), ADR 008 (workflow-level evals), ADR 016 (the continuous-eval scheduler — same scheduler primitive), ADR 014 (durable agent registry — what gets orchestrated),
`src/movate/core/workflow/` (`WorkflowRunner`, `NodeType`, the `HUMAN`/HITL stub), `JobKind` + the Postgres job queue + the KEDA worker (`infra/azure/modules/containerapp-worker.bicep`)

---

## Decision

Support agent orchestration on Azure by **extending the native engine `mdk`
already has**, and treat external orchestrators (Airflow / Prefect / Temporal /
Dagster) as **adapters we integrate with, not frameworks we adopt**:

1. **(D1) Do NOT adopt Airflow (or any heavy orchestrator) as a core
   dependency.** `mdk` already has the substrate: a workflow DAG engine
   (`WorkflowRunner` + agent/intent-router/agent-as-tool nodes), a Postgres-backed
   **job queue** (`JobKind.AGENT/WORKFLOW/EVAL/BENCH`), **KEDA** queue-depth
   autoscaling, and retry/dead-letter. The gaps are **scheduling/triggers** and
   **durable/HITL execution** — not a missing orchestrator.
2. **(D2) Extend the native engine (primary path):** add a **scheduler**
   (cron → enqueue agent/workflow jobs), **event/webhook triggers**, and
   **durable + HITL** execution (finish the stubbed `HUMAN` pause/resume node).
   Portable: on Azure the scheduler rides **Container Apps Jobs** (native
   cron/event) enqueuing into the existing queue; the KEDA worker executes.
3. **(D3) Adapt to external orchestrators (secondary path):** expose movate
   agents/workflows as **callable tasks** over the existing `/api/v1` + `mdk
   submit` async API, plus **thin OPTIONAL** integration packages (a Prefect
   task, an Airflow `MovateAgentOperator`, a generic webhook contract). movate
   stays the *callable*, never the *dependent* — so a customer who already runs
   an orchestrator drives movate from it without movate taking the dependency.
4. **(D4) If a single external engine is genuinely required** (durable,
   cross-system, very-long-running): **Temporal** (durable execution) or
   **Prefect** (lighter, dynamic) — **not Airflow** — behind an adapter, as an
   opt-in extra, with **Deva sign-off** (ADR 001).

In one sentence: **"orchestration is the native workflow+jobs+KEDA engine plus a
scheduler/triggers/HITL — and any external orchestrator integrates by *calling*
movate as a task, never by becoming a core dependency."**

---

## Context

"Orchestrate agents on Azure (Airflow/Prefect/…)" sounds like a build-vs-buy on
a workflow engine. But `mdk` already orchestrates in-platform multi-agent DAGs:

* **Workflow engine** — `src/movate/core/workflow/` (`WorkflowRunner.run`,
  `NodeType` incl. `INTENT_ROUTER` and a stubbed `HUMAN` = "v1.1 — HITL, runner
  pauses + persists state"), agent-as-tool skills, intent routing.
* **Async execution** — a Postgres job queue (`JobKind.{AGENT,WORKFLOW,EVAL,
  BENCH}`), the KEDA-autoscaled worker, retry + dead-letter.
* **Durable registry** (ADR 014) — versioned agents the orchestration runs.

What's genuinely missing for "orchestration on Azure":
1. **Scheduling** — there is no cron/timer; jobs are submit-triggered. (ADR 016
   adds a scheduler for *continuous eval* — the same primitive generalizes.)
2. **Event triggers** — no "run this agent when X happens" (webhook/queue event).
3. **Durable long-running / HITL** — the `HUMAN` pause/resume node is a stub.
4. **Interop** — customers who already run Airflow/Prefect/etc. have no clean
   "call a movate agent as a step" path beyond raw HTTP.

Adopting Airflow as core would be the wrong answer: it's a heavy stack
(scheduler + webserver + metadata DB + executor pools), batch/ETL- and
static-DAG-oriented, ill-suited to event-driven low-latency agent calls, and a
large dependency + ops burden that contradicts ADR 001 (portability) and the
"minimal dependencies / no framework sprawl without a proven scaling need" rule.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Reuse over adoption** — the workflow+jobs+KEDA engine already exists; close the *gaps* | HIGH |
| **Cloud portability (ADR 001)** — scheduler/triggers must be portable; external orchestrators stay optional + adapter-isolated; no Azure-only lock-in | HIGH |
| **Minimal deps / no framework sprawl** — don't take a heavy orchestrator dependency without a proven need | HIGH |
| **Interop** — let customers drive movate from the orchestrator they *already* run | MED |
| **Durable/long-running + HITL** — multi-step, human-gated agent pipelines need pause/resume | MED |
| **Operability** — scheduling + triggers should be observable + retryable (reuse the existing job machinery) | MED |

---

## Architecture

```
  TRIGGERS                         NATIVE ENGINE (exists today)            EXTERNAL (adapter)
  ────────                         ───────────────────────────            ──────────────────
  cron (ACA Jobs)  ─┐                                                      Airflow / Prefect /
  webhook/event    ─┼─▶ enqueue ─▶ Postgres job queue ─▶ KEDA worker ─┐    Temporal / Dagster
  mdk submit       ─┘   (D2/D3)    (JobKind.WORKFLOW/AGENT)            │         │ (D3/D4)
  external orch.   ────────────────────────────────────────────────────────────┘ calls
                                          │                            │   /api/v1 + mdk submit
                                          ▼                            │   via a thin OPTIONAL
                                   WorkflowRunner (DAG)                 │   MovateAgentOperator /
                                   ├─ agent / intent-router /          │   Prefect task
                                   │  agent-as-tool nodes              │
                                   └─ HUMAN node → pause + persist ◀───┘   (D4: Temporal/Prefect
                                      state; resume on webhook (D4)        only if a single engine
                                                                           is required; never core)
```

The orchestrator **is** movate's workflow engine + the job queue + a scheduler.
External engines sit *outside* and call in. The seams already exist (the queue,
the runner, the `/api/v1` async API); D2/D4 fill the scheduler/trigger/HITL gaps.

---

## Decisions

### Decision 1 (D1): Reject Airflow-as-core; the native engine is the orchestrator

`mdk`'s workflow DAG + Postgres queue + KEDA worker already orchestrate
multi-agent pipelines. Adopting Airflow would duplicate that with a far heavier,
worse-fit stack (batch/static-DAG, webserver+scheduler+metadata-DB+executors) and
a major dependency/ops cost — rejected per ADR 001 + the minimal-deps rule. We
close the *gaps* (scheduling/triggers/HITL) on the engine we have.

### Decision 2 (D2): Native scheduler + triggers, portable (ACA Jobs on Azure)

- **Scheduler:** a cron primitive that **enqueues agent/workflow jobs** on a
  schedule. This **generalizes ADR 016's continuous-eval scheduler** — same
  enqueue-on-cron mechanism, broader payload (`JobKind.WORKFLOW`/`AGENT`). On
  Azure it rides **Container Apps Jobs** (native cron/event); off-Azure any cron
  enqueues into the same queue. No new heavy dep.
- **Event/webhook triggers:** an inbound endpoint (or queue consumer) that
  enqueues a run on an event ("process this incoming ticket"). Reuses the job
  queue + auth (scopes, ADR 013).
- Both are **observable + retryable** for free (they become normal jobs).

### Decision 3 (D3): External orchestrators integrate by *calling* movate (adapter posture)

The default interop model: an external orchestrator drives movate as a **task**,
via the existing `/api/v1` (`POST /agents/{name}/runs`, `…/evals`, workflow runs)
+ `mdk submit` async API (submit → poll/`?wait=true` → fetch result). Ship **thin
OPTIONAL** glue, never a core dependency:
- a **Prefect** task/flow wrapper (`mdk[prefect]` extra) and an **Airflow**
  `MovateAgentOperator` (`mdk[airflow]` extra) — small wrappers over
  `MovateClient`;
- a documented **generic webhook/CLI contract** so Dagster / Azure Data Factory /
  Logic Apps / any tool can call movate with no movate-side code.
movate takes **no** orchestrator dependency; the integration lives in opt-in
extras + docs.

### Decision 4 (D4): A single external engine only if required — Temporal/Prefect, not Airflow

If a deployment genuinely needs an external engine for **durable, cross-system,
very-long-running** orchestration beyond what D2 covers, prefer **Temporal**
(durable execution — strongest fit for long/HITL agent workflows) or **Prefect**
(lighter, dynamic, Python-native, event-friendly). **Not Airflow.** It lands
**behind an adapter**, as an **opt-in extra**, isolated like ADR 001's
boto3/azure-storage carve-out, and **requires Deva sign-off** (it's a
cloud/infra-shaped dependency). The native engine (D2) remains the default and
the portable floor.

### Decision 5 (D5): Durable + HITL on the native runner

Finish the stubbed `HUMAN` node (`core/workflow/ir.py`): the `WorkflowRunner`
**pauses + persists state** at a human-gate node and **resumes on an external
signal** (webhook / Teams card button / API call) — the durable-execution
capability that long multi-agent pipelines need, on our own engine. (Coordinates
with the HITL backlog item #28 + ADR 003's Teams Adaptive Cards as a transport.)
This is design-first (a state/resume model) and the largest piece.

---

## Consequences

**Positive**
- Orchestration on Azure with **no heavy framework adoption** — reuses the
  workflow+jobs+KEDA engine; portable per ADR 001.
- Scheduling + triggers make agents **proactive** (cron/event-driven), not only
  request-driven — and they're observable/retryable as normal jobs.
- Customers keep their existing orchestrator and drive movate as a task (no
  lock-in, no forced migration).
- HITL/durable execution unlocks long, human-gated multi-agent pipelines.

**Negative / costs**
- Net-new: a scheduler + trigger surface + the durable/HITL state model — and the
  operational care they need (missed-run handling, idempotency, resume-after-crash).
- The optional adapter packages (`mdk[prefect]`/`mdk[airflow]`) add install-matrix
  surface to test (kept opt-in + thin).
- A future Temporal/Prefect adoption (D4) is real infra + a sign-off gate.

**Neutral**
- New config/surfaces (schedules, triggers, the adapter extras) — additive,
  default-off. The native workflow engine + `/api/v1` are unchanged.

---

## Implementation plan (separate PRs, after this ADR + the ADR 016 scheduler)

1. **Generalize the ADR-016 scheduler** to enqueue arbitrary agent/workflow jobs
   on a cron (not just continuous eval); ACA Jobs as the Azure substrate.
2. **Event/webhook triggers** — an inbound trigger that enqueues a run on an
   event; scope-gated (ADR 013).
3. **Durable + HITL** — implement the `HUMAN` pause/resume node + state
   persistence + resume-on-signal (design-first; ~2 PRs).
4. **External-orchestrator adapter pack** — `mdk[prefect]` task + `mdk[airflow]`
   `MovateAgentOperator` + the generic webhook/CLI contract + docs. (No core dep.)
5. *(Only if required)* a Temporal/Prefect durable-execution backend behind an
   adapter — **Deva sign-off** (ADR 001).

## Risks / open questions

- **Scheduler reliability** — missed runs on pod recycle, idempotency,
  catch-up/backfill semantics; lean on the durable Postgres queue + the existing
  retry/dead-letter rather than in-memory timers.
- **HITL state model** — where paused-workflow state lives (the registry/jobs
  store), resume-token security (scopes), and timeouts; the largest design risk.
- **Adapter maintenance** — Airflow/Prefect API churn in the optional packages;
  keep them thin (just call `MovateClient`) to minimize breakage.
- **Scope creep into a workflow platform** — keep D2 focused on
  scheduling/triggers/HITL; resist re-building a general DAG/ETL tool (that's what
  the external-orchestrator adapter is for).
