# ADR 082 — Temporal operational observability: runtime-tagged completion metric + workbook

**Status:** Proposed
**Date:** 2026-06-06
**Deciders:** Engineering (Movate)
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 078 (self-hosted Temporal on ACA — the backend this observes; the Temporal
**Web UI** D6 is the complementary per-workflow surface),
ADR 080 (Temporal execution completeness — the terminal activity
`persist_workflow_result_activity` this ADR hooks for the metric),
ADR 020 (OTel Collector → App Insights — the pipeline the metric rides; the
workbook queries the `App*` tables it lands in),
ADR 031 (dashboards-as-code + the metric-name drift guard this ADR extends),
ADR 034/035/039 (the existing `mdk.*` metric vocabulary + `METRIC_NAMES` source
of truth this adds one instrument to).

---

## Context

The self-hosted Temporal backend is deployed and running durable workflows (ADR
078/080). The **Temporal Web UI** (ADR 078 D6) lets an operator browse an
*individual* workflow's history, timing, and pending signals. What's missing is
the **aggregate operational view**: are durable workflows completing, at what
throughput, and at what success rate?

The blocker is that **Temporal-executed workflows emit no completion telemetry
today**. The native runner's terminal jobs are counted by
`mdk.jobs.completed{kind=WORKFLOW}` at the dispatch edge — but a `runtime:
temporal` workflow runs in the Temporal worker and **never touches that edge**.
It emits only its activities' `agent.execute` spans, which carry **no `runtime`
attribute**, so they can't even be distinguished from native agent execution in
App Insights. There is no signal a workbook could honestly chart as "durable
workflow throughput / success rate."

The user asked for a Temporal **dashboard** (operational metrics), choosing an
**Azure Monitor Workbook** surface (consistent with the four persona workbooks
already shipped via `monitor-workbooks.bicep`, App-Insights-backed).

## Decision

Add one runtime-tagged completion metric emitted from the Temporal terminal
activity, and a fifth Azure Monitor workbook built on it. Plus the wiring fix
that makes the Temporal worker export metrics at all.

### D1 — New instrument `mdk.workflow.completed`
A counter with attributes `{workflow, status, runtime, tenant}`, added to
`METRIC_NAMES` (the drift-guard source of truth) with a `record_workflow_completed()`
helper following the existing `record_job_completed` pattern (no-op when metrics
are off / OTel absent; never raises). `workflow` is the workflow **name**
(bounded, low-cardinality like `kind` — never the run_id). `runtime` is the
backend that executed it.

### D2 — Emit from the Temporal terminal activity
`persist_workflow_result_activity` (ADR 080) is the one place every durable
workflow reaches a terminal state in the Temporal worker. It now also calls
`record_workflow_completed(..., runtime="temporal")` after persisting the
terminal `WorkflowRunRecord`. The emit is **fail-soft + lazily imported**: the
record is the source of truth, the metric is best-effort telemetry, and a
metrics hiccup must never fail the terminal persist.

We deliberately **do not** emit it from the native runner in this ADR: native
workflows are already covered by `mdk.jobs.completed{kind=WORKFLOW}`. The
`runtime` attribute is kept on the new instrument anyway so a native emitter can
*later* share it for a true backend-comparison view without a schema change —
until then the metric is temporal-only and the workbook filters
`runtime == "temporal"` for forward-compatibility.

### D3 — Initialize metrics on the Temporal worker
The Temporal worker path (`mdk worker --backend temporal`) **never called**
`init_metrics()` / `install_log_correlation()` / pool-gauge registration — so it
exported no metrics at all. We add the same three-line startup wiring the native
`mdk worker` already has (all no-ops when the OTel extra/sink is absent). Without
this, D1/D2 would silently never export. This also lights up the asyncpg
pool-saturation gauges (ADR 034) on the Temporal worker for free.

### D4 — A fifth workbook: `temporal.workbook.json`
A persona workbook (`infra/azure-monitor/workbooks/temporal.workbook.json`)
wired into `monitor-workbooks.bicep` alongside the existing four, riding the same
`enableWorkbooks` gate. It is built **entirely on `mdk.workflow.completed`**:
health tiles (total / failed / success%), throughput by status, completions +
success% by workflow, and a failure-by-workflow trend. It is **honest about its
boundaries**: per-workflow timing/history → the Temporal Web UI; task-queue
depth / worker poll health / per-workflow duration histograms → not yet wired
(a documented follow-on to pipe the Temporal SDK's built-in metrics through the
same Collector). The workbook is added to the ADR-031 drift guard
(`test_dashboards_metric_names.py`), bringing it under anti-drift protection.

## Consequences

**Positive**
- Durable workflows get a first-class throughput + success/failure signal,
  surfaced in a portal-native workbook consistent with the existing four.
- The Temporal worker now exports *all* mdk metrics (workflow completion + the
  pool gauges), closing a silent observability hole.
- One small, low-cardinality instrument; fail-soft emit; the native path and all
  existing metrics are untouched.

**Negative / trade-offs**
- The workbook's coverage is bounded by what we emit today (completion counts +
  rates). Latency, task-queue depth, and worker-poll health need the Temporal
  SDK-metrics follow-on — called out explicitly in the workbook itself so it
  doesn't read as "fully covered" when it isn't (CLAUDE.md no-silent-caps).
- `workflow` as an attribute adds cardinality bounded by the number of deployed
  workflow names — acceptable (same philosophy as `kind`), but a tenant with
  thousands of distinct workflow names would inflate series count. Documented.

## Alternatives considered
- **Stamp `runtime` on `agent.execute` spans and chart those.** Would require
  threading the backend through the Executor (which is backend-agnostic by
  design, ADR 054 D3) and still wouldn't give a workflow-level completion
  signal. Rejected — invasive to core execution logic for a worse signal.
- **Reuse `mdk.jobs.completed` for temporal.** The temporal path doesn't go
  through the job/dispatch edge that emits it; forcing it there would mean faking
  a job lifecycle. Rejected.
- **Wire the full Temporal SDK Prometheus metrics now.** Higher-value long-term
  (task-queue depth, poll health) but a bigger lift (a metrics exporter on the
  SDK + Collector scrape config). Deferred as the explicit follow-on; this ADR
  ships the workflow-completion backbone first.
- **Grafana instead of a workbook.** The user chose the workbook surface (no new
  service; matches the four existing persona workbooks). A Grafana panel can be
  added later from the same instrument.

## Compatibility (CLAUDE.md rule 5)
Additive. One new OTel instrument (`mdk.workflow.completed`) — purely additive to
the metric vocabulary; no existing metric changes. New worker-startup calls are
no-ops when observability is off. New workbook rides the existing `enableWorkbooks`
flag (no new deploy flag). No change to `agent.yaml`/`project.yaml`, the
`/api/v1` shapes, storage schema, CLI flags, or env vars. Native execution and
all existing dashboards are byte-for-byte unchanged.
