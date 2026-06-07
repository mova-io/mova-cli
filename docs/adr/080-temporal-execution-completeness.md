# ADR 080 — Temporal execution completeness: deployed worker + terminal-state sync (and the durable-workflow rollout backlog)

**Status:** Proposed
**Date:** 2026-06-06
**Deciders:** Engineering (runtime / infra) — **no new shipped dependency; builds
on the already-adopted `temporalio` opt-in (ADR 054/065).**
**Builds on / composes with (changes nothing in any of them):**
ADR 054 (Temporal as the durable backend — compiler, activities, `run_temporal_worker`),
ADR 055 (runtime dispatch fork — native/langgraph/temporal),
ADR 062 (durable HITL HUMAN node — `wait_condition` + signal; landed in PR #710),
ADR 065 (Temporal as the optional durable-execution seam — native is the floor),
ADR 078 (self-hosted Temporal server on Azure Container Apps — PR #710),
ADR 077 (agent→workflow dispatch skill — PR #713).

**Defining gap (empirical, from the Phase 2 build).** Phase 1 (compiler,
activities, dispatch fork, replay, conformance) is shipped; Phase 2 durable HITL
(ADR 062) and the Temporal-server-on-ACA deploy (ADR 078) are in open PRs. Yet a
`runtime: temporal` workflow **still cannot run to completion in a deployed
environment**, for two reasons:

1. **No deployed Temporal *worker*.** ADR 078 deploys the Temporal *server* and
   wires `TEMPORAL_HOST` into the API + worker Container Apps, but
   `containerapp-worker.bicep` runs the **native job-queue worker**
   (`movate worker`), not `mdk worker --backend temporal`. Nothing polls the
   `mdk-workflows` task queue, so a compiled durable workflow is registered with
   no process to execute it.
2. **No terminal-state sync.** The Temporal execution path persists per-node
   `RunRecord`s (the activities reuse `Executor`) and the *pause* record
   (`call_human_activity`), but **never writes the terminal
   `WorkflowRunRecord`** (SUCCESS/ERROR + `final_state`) back to the
   `StorageProvider`. So `mdk runs show` returns stale state and — worse — a
   resumed HITL run stays `PAUSED` in the store forever, so the
   `GET …/workflow-runs?status=paused` approvals list never clears after a human
   responds. This directly undercuts the durable-HITL feature ADR 062 adds.

This ADR closes the **execution-completeness** gap (the two blockers above) and
records the **remaining durable-workflow rollout backlog** so the gaps are
tracked. It is a **deployment-lifecycle + runtime** ADR (rule 2); the changes are
additive (rule 5 flagged) and gated on the existing `enableTemporal` flag /
`runtime: temporal` opt-in — native is unaffected.

---

## Decision

### D1 — Deploy a Temporal worker as its own Container App

Add `infra/azure/modules/containerapp-temporal-worker.bicep`: a Container App
running the existing `mdk worker --backend temporal` (already implemented —
`cli/worker.py` → `workflow_backend.run_temporal_worker`). It reuses the runtime
image, the worker UAI (ACR pull + KV read), and the shared Postgres/LLM secrets,
and reads `TEMPORAL_HOST`/`TEMPORAL_NAMESPACE` (the BYOK seam, ADR 054 D5). No
ingress (it polls the Temporal task queue, not HTTP).

Gated `if (enableTemporal && enableApiWorker)` and wired in `main.bicep` next to
the ADR 078 server module. **A separate Container App** (not a flag on the
existing native worker) because the two have different lifecycles and scaling
signals: the native worker scales on the Postgres KEDA queue-depth scaler; the
Temporal worker's load lives in the Temporal service, which KEDA can't read the
same way. Scaling for v1 is a small fixed replica count (1–2); a
Temporal-metrics-driven scaler is Tier 2 (below).

> Why not run both backends in one worker process? `movate worker` (native job
> queue) and `mdk worker --backend temporal` are distinct poll loops against
> distinct substrates. Keeping them as separate Container Apps preserves the
> single-responsibility-per-app pattern the rest of the CAE follows (api /
> worker / scheduler / langfuse / temporal-server) and lets each scale and fail
> independently.

### D2 — Persist the terminal `WorkflowRunRecord` from inside the workflow

The compiler emits a **terminal-persistence boundary** around the dispatch loop:
on completion (success *or* a handled error) the workflow calls a new
`persist_workflow_result_activity(run_id, status, final_state, error)` that
writes the terminal `WorkflowRunRecord` via `ctx.storage.save_workflow_run(...)`
— mirroring the native runner's end-of-run write and **clearing the HITL
`human_task["signaled"]` / `paused` markers** so the approvals list and
`mdk runs show` reflect reality.

```python
# emitted around the run body (sketch):
try:
    ...dispatch loop...
    await workflow.execute_activity(persist_workflow_result_activity,
        args=[run_id, "success", state, None], ...)
    return state
except ApplicationError as exc:
    await workflow.execute_activity(persist_workflow_result_activity,
        args=[run_id, "error", state, _error_info(exc)], ...)
    raise
```

This keeps the IO in an **activity** (ADR 054 D10: control flow in history, side
effects in activities), is durable + replay-safe, and works for both the
ephemeral path (`run_temporal_workflow`) and the long-lived worker (D1) without a
worker-side completion callback (Temporal doesn't give the long-lived worker a
per-workflow "done" hook — the workflow persisting its own terminal state is the
idiomatic seam). The ephemeral path additionally returns the `WorkflowResult` to
its caller as today.

### D3 — Validate the ADR 078 deploy assumptions (acceptance gate)

Two ADR 078 assumptions are unverified and gate "durable workflows work in
Azure": (a) ACA **internal TCP ingress ↔ Temporal frontend gRPC :7233**
interoperate; (b) the `temporalio/auto-setup` **Postgres-TLS env names** match
the pinned image tag against Azure Postgres (SSL-required). This ADR makes a real
`enableTemporal=true` deploy + an end-to-end run (HUMAN-node workflow: dispatch →
pause → signal → resume → terminal SUCCESS in the store) the **acceptance gate**
for marking the durable path production-usable. Tune the bicep env if the spike
finds drift.

### D4 — Conformance: add a deployed-shape end-to-end

Extend the conformance suite (ADR 055 D7) with a test that runs a workflow on the
long-lived-worker shape (register on a task queue, execute, then assert the
**store** holds the terminal `WorkflowRunRecord` matching the native runner's
final state) — not just the in-process final-state comparison. This locks D2 in
as a regression gate.

---

## Remaining durable-workflow rollout backlog (tracked here)

Tier 0 above is the critical path. The rest, by theme — captured so the gaps are
visible (roadmap.yaml entries to follow once its working-tree state is clean):

**Tier 1 — make durable HITL usable (finish ADR 062 / ADR 077 D3)**
- **Delivery adapter** — a `Notifier` Protocol + one Teams/ServiceNow card so a
  paused HUMAN node actually *reaches* an approver (today it's a silent paused
  row).
- **Routed HUMAN branching** — HUMAN advances to its sequential successor on both
  backends today (native parity). Real approval flows want `routes`
  (approve→X / reject→Y); needs a matching native-runner change to stay in parity.

**Tier 2 — production hardening (ADR 054 Phase 3)**
- **Per-node retry / timeout / heartbeat from `workflow.yaml`** (today hardcoded:
  300s schedule-to-close, 3 retries, no heartbeat). **Activity heartbeating for
  long LLM calls** is the urgent one — a >timeout LLM call is killed as stale.
- **Workflow versioning / patching** (`workflow.patched`) — long-running
  workflows break determinism on redeploy without it. Latent until long runs +
  frequent deploys coincide; write a determinism/versioning ADR as a tripwire.
- **HA + queue topology** — the ACA Temporal server is a single non-HA replica
  (ADR 078 D6); one task queue, no priority lanes (incident vs. batch-eval).

**Tier 3 — DX & observability**
- **`mdk auth login temporal`** (ADR 054 D8) + Temporal Cloud onboarding + a
  local-dev guide (`temporal server start-dev`). Recommend Temporal Cloud as the
  *default* path (lower ops burden); ADR 078 self-host is the data-residency case.
- **`mdk runs show` ↔ Temporal Web deep-link** (same `run_id`, ADR 054 D6),
  **Temporal SDK metrics → the OTel collector** (ADR 020), and **workflow-body
  spans** (the dispatch loop's branch decisions are currently untraced; only
  activities are).

**Tier 4 — expansion (the ADR 065 D4 adoption ladder)**
- Child workflows / composition (`temporal-composition`, planned); signals/queries
  beyond HUMAN; then durable eval/bench runs and sagas for the Azure deploy
  lifecycle / canary / continuous-eval.

---

## Consequences

**Positive**
- Durable workflows actually **run end-to-end in a deployed environment** (D1) and
  the mdk store stays the **single source of truth** for run state (D2) — fixing
  the stale-`mdk runs show` and never-clearing-approvals bugs that would
  otherwise ship with ADR 062.
- Additive + opt-in: zero effect when `enableTemporal=false` / `runtime: native`.
- The terminal-persistence seam (D2) is one place, reused by both execution paths
  and verified by conformance (D4).

**Negative / risks**
- A second worker Container App is one more thing to deploy/scale/observe (D1);
  mitigated by reusing the worker image + UAI.
- D2 persists terminal state from within the workflow — a failure of the
  persist activity itself is retried by Temporal, but a permanently-failing store
  would leave a completed workflow with no terminal record (logged + alertable;
  Temporal history is still the durable truth).
- The deploy spike (D3) may surface ACA/Temporal interop friction that needs
  bicep tuning before the path is production-usable.

## Alternatives considered
- **Worker-side completion listener** (the long-lived worker writes terminal
  state after each run): rejected — the long-lived Temporal worker has no
  per-workflow completion callback; a Temporal *interceptor* could approximate it
  but is more complex and less durable than the in-workflow activity (D2).
- **A final-node convention in `workflow.yaml`**: rejected — terminal persistence
  is a runtime invariant, not an authoring concern; emitting it in the compiler
  keeps every workflow correct by construction.
- **Flag on the existing native worker** instead of a separate Container App:
  rejected (D1 rationale) — different substrate, lifecycle, and scaling signal.

## Boundaries (out of scope)
- Everything in Tiers 1–4 above (tracked, not decided here).
- The native runner is unchanged (it already writes terminal records).

## New surfaces (CLAUDE.md §5 — all additive)
- New `containerapp-temporal-worker.bicep` module + `main.bicep` wiring (gated on
  `enableTemporal`). Zero effect when off.
- New `persist_workflow_result_activity` (registered by both Temporal worker
  paths) + the compiler's terminal-persistence emission. No change to
  `agent.yaml`/`workflow.yaml` schema, the `/api/v1` surface, storage schema, CLI
  flags, or env vars.
