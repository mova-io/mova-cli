# ADR 089 — Non-blocking Temporal dispatch for durable HITL workflows

- Status: Proposed
- Date: 2026-06-08
- Related: ADR 054 (Temporal Track B/C), ADR 055 (runtime dispatch fork),
  ADR 062 (durable HITL / HUMAN node), ADR 080 (Temporal terminal-state sync),
  ADR 078 (self-hosted Temporal). Issue #759.

## Context

`runtime: temporal` workflow jobs are dispatched by
`WorkflowDispatcher._execute_workflow` → `run_temporal_workflow`
(`src/movate/runtime/workflow_backend.py`), which does:

```python
result = await client.execute_workflow(workflow_cls.run, run_state, id=wf_id, ...)
```

`execute_workflow` **starts the workflow AND awaits its terminal result**. For a
durable HITL workflow (a HUMAN node, ADR 062) the workflow parks at
`wait_condition` until a human signals — which can be **minutes to days**. The
awaiting `execute_workflow` call holds the dispatcher's worker slot for that
entire wait.

**Observed 2026-06-08:** running the `refund-approval` demo several times without
signalling left the queue worker with multiple blocked `execute_workflow` calls.
With limited job concurrency, a fresh **native** agent job sat `queued` and never
ran — the paused durable workflows had **starved the queue**. Seven paused runs
accumulated, each pinning a slot. This defeats the whole point of durable
execution: a Temporal workflow is supposed to outlive its caller, not hold the
caller hostage.

The result path is **already decoupled** from this blocking call:
- `call_human_activity` writes a `PAUSED` `WorkflowRunRecord` when the workflow
  parks (listable via `GET /workflow-runs?status=paused`).
- On resume, `persist_workflow_result_activity` (ADR 080) writes the terminal
  `SUCCESS`/`ERROR` `WorkflowRunRecord`.

So the run record reflects the full pause→resume→terminal lifecycle **without**
the dispatcher awaiting anything. The blocking `execute_workflow` is redundant
for the durable case — it only re-derives a result the workflow already persists.

## Decision

For a `runtime: temporal` workflow whose graph contains a **HUMAN node**, dispatch
**non-blocking**: `client.start_workflow(...)` (returns a handle, does not await),
then return immediately with a `PAUSED`/accepted outcome. The run-record lifecycle
(ADR 062 pause record + ADR 080 terminal sync) is the source of truth for the
result. Non-HITL temporal workflows are **unchanged** (`execute_workflow`,
inline result) to preserve the job→output contract for the common case.

### D1 — HUMAN-node detection at dispatch time

The compiled `WorkflowGraph` already knows its node types. Branch on
`any(n.type == NodeType.HUMAN for n in graph.nodes.values())`. No new metadata.

### D2 — Non-blocking start returns an accepted outcome

`run_temporal_workflow` (HITL branch):
```python
await client.start_workflow(workflow_cls.run, run_state, id=wf_id, task_queue=...)
return dict(run_state), WorkflowStatus.PAUSED, None
```
`_workflow_result_to_outcome` maps `PAUSED` to a non-terminal `DispatchOutcome`
(the job is "accepted/started", not failed). The worker slot frees immediately;
the durable workflow runs on Temporal and resumes on signal via the **already
shipped** `POST /workflow-runs/{id}/signal` → `handle.signal("human_response", …)`
path (ADR 062 D2). The in-process worker that hosts the workflow activities is the
long-lived `mdk worker --backend temporal` — NOT the dispatching queue worker —
so nothing needs to stay resident in the dispatcher for the workflow to make
progress.

### D3 — Idempotent start

Use `id=workflow_run_id` (already the contract, ADR 054 D6) with
`id_reuse_policy` rejecting duplicates, so a re-dispatched job doesn't spawn a
second execution.

## Consequences

**Compat / blast radius (rule 5):**
- **HITL temporal jobs** change semantics: the job completes as **accepted** the
  moment the workflow is started + parked, instead of blocking until resume. The
  `?status=paused` inventory + the signal endpoint + the terminal `WorkflowRunRecord`
  are unchanged — every run-record-based consumer (CLI `mdk workflow runs`, the
  smoke `scripts/temporal-e2e-smoke.sh`, the Temporal UI) is unaffected. A caller
  that *blocked on the job result* for a HITL workflow (none today) would need to
  poll the run record instead.
- **Non-HITL temporal jobs**: byte-for-byte unchanged.
- **Native / LangGraph**: untouched.
- No storage-schema, `/api/v1`, CLI-flag, or env change.

**Why not "always non-blocking":** a non-HITL workflow with no pause completes in
seconds; blocking there is cheap and keeps the inline result (the job output IS
the workflow output). Only the unbounded-wait HITL case is pathological. Scoping
to HUMAN-node graphs is the minimal change that fixes the starvation without
altering the common-case contract.

**Follow-on (out of scope):** per-tenant worker concurrency / a dedicated HITL
dispatch lane (production-hardening, roadmap). This ADR removes the unbounded
hold; concurrency tuning is separate.

## Verification

- Unit (time-skipping `WorkflowEnvironment`, the ADR 055 D7 seed): a 3-node
  agent→HUMAN→agent workflow dispatched via the non-blocking path returns an
  accepted outcome **immediately** (no await), parks at the HUMAN node, and a
  subsequent `handle.signal` drives it to the SAME terminal state the blocking
  path produced.
- Regression: a non-HITL temporal workflow still returns its inline result
  (`execute_workflow` path) unchanged; `tests/test_temporal_execution.py` green.
- Operational: submit N `refund-approval` runs without signalling → native agent
  jobs still run (queue not starved); `?status=paused` lists N; signalling drains.
