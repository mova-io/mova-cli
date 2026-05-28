# Azure Monitor Workbooks for mdk

Operator-runbook companion to the Grafana dashboards under `dashboards/grafana/`.
The Workbooks live in `infra/azure-monitor/workbooks/` as JSON exports of the
Azure Monitor Workbook editor; they query the **workspace-based App Insights
`App*` tables** (the OTel Collector's `azuremonitor` exporter writes there - see
ADR 020 + `infra/azure/modules/containerapp-otel-collector.bicep`).

The four files are **persona-scoped runbooks**, not generic dashboards: each
section in each workbook follows the same **What / Normal / If red, do** pattern
as the prescriptive Grafana dashboards.

## Files

| Workbook | Persona | Covers |
| --- | --- | --- |
| `infra/azure-monitor/workbooks/operator.workbook.json` | On-call operator | Health overview, active alerts, recent failures, latency heatmap, throughput by status |
| `infra/azure-monitor/workbooks/platform.workbook.json` | Platform / SRE team | Postgres pool (ADR 034 D3), queue depth + in-flight age, Container Apps revisions + restarts, cost & tokens |
| `infra/azure-monitor/workbooks/eval-and-drift.workbook.json` | Eval engineer / model owner | Eval pass-rate + drift signal + canary status - **scaffolded only**: `mdk eval` emits Langfuse scores today, not OTel metrics (see ADR 031 D1). The workbook documents the schema for when these instruments land (ADR 016 D3 / ADR 031 follow-ups). |
| `infra/azure-monitor/workbooks/tenant-ops.workbook.json` | Tenant operator / customer-success | Per-tenant slice: requests / latency / errors / cost / tokens. Uses a workbook parameter `Tenant` populated by a `distinct` query against `AppMetrics.Properties.tenant`. |

The four item-27 SLO alert rules (`*-deadletter-spike`, `*-high-error-rate`,
`*-high-latency-p95`, `*-availability-no-traffic` from
`infra/azure/modules/monitor-alerts.bicep`) are referenced by name in every
relevant section's "If red, do" guidance, so on-call can pivot from a fired
alert straight to the matching workbook section.

## How to import (portal, today)

1. **Azure Portal** -> *Monitor* -> *Workbooks* -> **+ New**.
2. Top toolbar: click `</>` (**Advanced Editor**).
3. Change the editor dropdown to **Gallery Template** mode.
4. Paste the entire contents of one of the JSON files from
   `infra/azure-monitor/workbooks/`.
5. Click **Apply**, then **Done Editing**.
6. **Save**: pick your subscription, resource group, and (critically) **the Log
   Analytics workspace** the deployment writes to as the workbook's resource -
   the KQL items use `resourceType: microsoft.operationalinsights/workspaces`,
   so a workspace scope is required for the queries to bind. (The same scope
   the existing `dashboards/azure/mdk-golden-signals.workbook.json` uses; see
   `dashboards/README.md`.)
7. Save as: `mdk - <persona>` (e.g. `mdk - operator`, `mdk - platform`).

The `Tenant` parameter on `tenant-ops.workbook.json` populates from a `distinct`
KQL query the first time the workbook loads; pick a tenant from the dropdown
and the other panels refresh.

## How to deploy as code (future, NOT this PR)

Azure provides a `Microsoft.Insights/workbooks` ARM/Bicep resource that lets
these JSON files be deployed declaratively (`serializedData` property carries
the JSON string). We **deliberately do not add Bicep wiring in this PR** - the
existing `infra/azure/modules/` is reviewed as the deploy surface and adding
four workbook resources would mix two responsibilities (CLAUDE.md rule 3).

When that's the right next step, the shape will be roughly:

```bicep
resource workbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(workspaceResourceId, 'mdk-operator')
  location: location
  kind: 'shared'
  properties: {
    displayName: 'mdk - operator'
    serializedData: loadTextContent('../azure-monitor/workbooks/operator.workbook.json')
    sourceId: workspaceResourceId
    category: 'workbook'
  }
}
```

Drop one of these per persona, all keyed on the same workspace. Tag them with
`movate:appInsightsId` to match `monitor-alerts.bicep`'s convention.

## Where each persona's workbook fits in the on-call flow

```
        +-------------------+
SLO --> | operator.workbook |  "what's wrong right now?"
fired   +-------------------+
            |       |
   (latency)|       |(error spike)
            v       v
    +-------------------+      +--------------------------+
    | platform.workbook | <--- | revisions / pool / queue |
    +-------------------+      +--------------------------+
            |
   (model regression suspected)
            v
    +--------------------------+
    | eval-and-drift.workbook  | (scaffolded; live data
    +--------------------------+  in Langfuse today)
            |
   (single-tenant complaint)
            v
    +-----------------------+
    | tenant-ops.workbook   |  scope=tenant dropdown
    +-----------------------+
```

Operator opens `operator.workbook.json` first - it surfaces the active SLO
alerts and the top-line failures. From there:

- A **platform-substrate** problem (DB pool, autoscale, restarts) -> pivot to
  `platform.workbook.json`.
- A **model behavior** suspicion -> open `eval-and-drift.workbook.json`. Note
  the disclaimer at the top of that workbook: the actual eval scores live in
  Langfuse today (ADR 031 D1).
- A **per-tenant** ticket -> open `tenant-ops.workbook.json` and pick the
  tenant.

## Grafana alternative for the same data

Every panel here has a Grafana counterpart under `dashboards/grafana/` that
renders the same OTel metric (just via PromQL instead of KQL). See the
prescriptive layer on those dashboards - each chart has a "Sub-panel: triage
notes" text panel with the same **What / Normal / If red, do** pattern as the
sections here. The Grafana dashboards also include drill-down links to the
local Jaeger demo and back to these Workbooks.

| Persona | Workbook | Grafana dashboard |
| --- | --- | --- |
| Operator | `operator.workbook.json` | `dashboards/grafana/mdk-golden-signals.json` |
| Platform | `platform.workbook.json` | `dashboards/grafana/mdk-queue-and-pool.json` + `dashboards/grafana/mdk-cost.json` |
| Eval | `eval-and-drift.workbook.json` | _(no Grafana mirror today - same data not in Prometheus either; both surfaces empty until ADR 031 follow-up lands)_ |
| Tenant ops | `tenant-ops.workbook.json` | `dashboards/grafana/mdk-runtime-overview.json` (with `$tenant` variable) |

## When to use which surface

- **Workbooks**: native Azure, shares auth with the Portal, KQL is the right
  query language when you need to pivot to Activity Log / Resource Graph (e.g.
  to correlate a deploy with an alert), and it's the same identity that owns
  the alert rules.
- **Grafana**: open-source, multi-cloud, supports the local demo stack
  (`infra/otel-collector/`) where Workbooks can't run, and the prescriptive
  layer includes **live Jaeger drill-down links** which Workbooks can't replace
  without leaving the Portal.

In practice teams that live in Azure Portal default to Workbooks; teams that
want a single multi-cloud pane default to Grafana. Both render the same metrics
because mdk's catalog (`src/movate/tracing/metrics.py`, `METRIC_NAMES`) is the
single source of truth; the two anti-drift tests
(`tests/test_grafana_dashboards.py`, `tests/test_dashboards_metric_names.py`)
keep both surfaces honest against it.
