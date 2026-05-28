# Deploying MDK's Azure Monitor Workbooks

This runbook covers deploying the prescriptive Azure Monitor Workbooks that
ship with `mdk` as Bicep. The Workbooks render the same Log Analytics signals
the in-tree alert rules already evaluate (see
`infra/azure/modules/monitor-alerts.bicep`), giving on-call and platform-eng a
portal-native view alongside the alerting pipeline.

> **Status (preview).** The Bicep wrapper at
> `infra/azure-monitor/workbooks.bicep` is shipped with the resource blocks
> commented out, waiting on the four prescriptive Workbook JSONs to land at
> `infra/azure-monitor/workbooks/{operator,platform,eval-and-drift,tenant-ops}.workbook.json`.
> PR #518 ships three Grafana dashboards and one Azure Workbook
> (`dashboards/azure/mdk-golden-signals.workbook.json`) — the four prescriptive
> Workbooks this module wraps are produced by a follow-up. Once the JSONs are
> on `main`, the resource blocks in `workbooks.bicep` can be uncommented and
> this runbook applies as written.

## Pre-reqs

* An existing **workspace-based Application Insights** component writing to a
  **Log Analytics workspace**. If you deployed `mdk` via
  `infra/azure/main.bicep` with `enableAppInsights=true`, this is the
  workspace that `logs.outputs.workspaceId` points at — the same one
  `monitor-alerts.bicep` scopes its alert rules to.
* RBAC: **`Monitoring Contributor`** on the target resource group. This role
  carries `Microsoft.Insights/workbooks/write` (and read), which is the
  minimum to create / update Workbooks. `Contributor` on the RG also works
  but is overpermissioned for the task.
* `az` CLI logged in to the target subscription
  (`az account set --subscription <id>`).

## Finding your workspace id

```bash
az monitor log-analytics workspace show \
  --resource-group <rg> \
  --workspace-name <workspace-name> \
  --query id -o tsv
```

The output looks like
`/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.OperationalInsights/workspaces/<name>`.
Pass it as `logAnalyticsWorkspaceId` to the deployment.

## Deploy

Single command:

```bash
az deployment group create \
  --resource-group <rg> \
  --template-file infra/azure-monitor/workbooks.bicep \
  --parameters logAnalyticsWorkspaceId=<workspace-resource-id>
```

Or, with a parameters file (see
`infra/azure-monitor/workbooks.parameters.example.json` for the shape):

```bash
az deployment group create \
  --resource-group <rg> \
  --template-file infra/azure-monitor/workbooks.bicep \
  --parameters @workbooks.parameters.json
```

Once the prescriptive Workbook JSONs land, the deployment provisions four
`Microsoft.Insights/workbooks` resources — one each for **Operator**,
**Platform**, **Eval & Drift**, and **Tenant Ops** — all named
deterministically via `guid(resourceGroup().id, '<stable-key>')` so re-running
the deployment updates in place rather than duplicating. They appear in the
Workbooks gallery grouped by the `namePrefix` (default `MDK · `).

## What if the JSON drifts?

`workbooks.bicep` uses `loadTextContent(...)` to inline each Workbook JSON at
template-compile time. That means:

* Edit `infra/azure-monitor/workbooks/<name>.workbook.json` directly.
* Re-run `az deployment group create` with the same parameters.
* The Workbook's `serializedData` is updated in place — no separate "publish"
  step, no portal-side editing required.

Treat the JSONs as the source of truth. If an operator hand-edits a Workbook
in the portal, the next `az deployment group create` will overwrite their
edits — which is the intended behavior (Workbooks-as-code).

## Uninstalling

`az deployment group delete` removes the **deployment record** but leaves the
created Workbook resources in place (deployments in Azure are not
"installations"). To remove the Workbooks themselves:

```bash
az monitor app-insights workbook delete \
  --resource-group <rg> \
  --name <workbook-resource-name>
```

The resource names are the GUIDs that `guid(resourceGroup().id, '<key>')`
produces; list them first with:

```bash
az monitor app-insights workbook list \
  --resource-group <rg> \
  --query "[?starts_with(displayName, 'MDK ')].{name:name, displayName:displayName}" \
  -o table
```

## Roadmap: `mdk deploy --with-dashboards` (future work)

A `--with-dashboards` flag on `mdk deploy` would wire this Bicep into the
main per-tenant Azure deployment so Workbooks are provisioned alongside the
Container Apps and App Insights component. That's a **compat-sensitive**
change to `mdk deploy` (touches `src/movate/cli/deploy.py`, adds a new CLI
flag, may want a per-tenant on/off switch) and is **out of scope for this
PR** — tracked separately. Until then, the deployment above is a clean,
standalone follow-on the operator runs once per tenant RG.
