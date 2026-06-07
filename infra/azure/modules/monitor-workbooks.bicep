// Azure Monitor Workbooks (item 27 companion) — the Azure-native parallel to the
// in-repo Grafana dashboards (dashboards/grafana/). Deploys the four prescriptive
// persona Workbooks as `Microsoft.Insights/workbooks` resources, so on-call,
// platform-eng, eval-owners, and tenant-ops each get a portal-native runbook over
// the SAME OTel catalog the Grafana dashboards render.
//
// SIGNAL SOURCE — workspace-based App Insights (identical to monitor-alerts.bicep):
//   The App Insights component (modules/appinsights.bicep) is workspace-based
//   (IngestionMode=LogAnalytics), so the OTel Collector's `azuremonitor` exporter
//   (ADR 020) lands the runtime's OTLP in the EXISTING Log Analytics workspace
//   under the `App*` tables:
//     - spans   → AppDependencies (Name == span name, e.g. "agent.execute")
//     - metrics → AppMetrics      (Name == the OTel instrument name verbatim,
//                                   e.g. "mdk.jobs.completed"; attrs in Properties)
//     - requests→ AppRequests     (HTTP server spans, e.g. /healthz, /api/v1)
//   Every Workbook's KQL targets the WORKSPACE (resourceType
//   microsoft.operationalinsights/workspaces), so each resource's `sourceId` is
//   the workspace id — the same `logs.outputs.workspaceId` the alert rules scope to.
//
// WORKBOOKS-AS-CODE — the canonical Azure pattern:
//   * `serializedData` carries the Workbook JSON as a string. `loadTextContent()`
//     inlines each JSON at template-COMPILE time, so the JSON files under
//     infra/azure-monitor/workbooks/ stay the single source of truth: edit the
//     JSON, re-run the deployment, and the deployed Workbook refreshes in place.
//   * `name` is a deterministic guid(resourceGroup().id, '<stable-key>') so a
//     re-deploy UPDATES in place rather than creating duplicates.
//   * `kind: 'shared'` makes each Workbook visible to anyone with read on the RG
//     (vs `kind: 'user'`, which scopes it to one principal).
//
// API VERSION 2023-06-01 — current GA; matches the Microsoft.Insights pins used
// by monitor-alerts.bicep / appinsights.bicep.
//
// DEFAULT-OFF: invoked from main.bicep only when (enableWorkbooks && enableAppInsights).
// When enableWorkbooks=false, main.bicep does not instantiate this module at all,
// so ZERO workbook resources are emitted and the template is unchanged.
//
// SCOPE NOTE: this module ships the four persona Workbooks (operator / platform /
// eval-and-drift / tenant-ops) PLUS the temporal operational Workbook (ADR 082).
// The intelligence-layer JSON, infra/azure-monitor/workbooks/insights.workbook.json,
// is an intelligence-layer Workbook whose narrative panels read the ADR-047
// Observability Intelligence API (NOT an App* table), so it is intentionally left
// out of this deploy surface.

@description('Resource id of the EXISTING Log Analytics workspace the workspace-based App Insights writes its App* tables to. Each Workbook\'s `sourceId` is set to this, and the KQL items bind to it. Passed from logs.outputs.workspaceId in main.bicep — the same workspace monitor-alerts.bicep scopes to.')
param workspaceResourceId string

@description('Azure region for the Workbook resources. Microsoft.Insights/workbooks are RG-scoped, regional resources. Defaults to the RG location.')
param location string = resourceGroup().location

@description('Display-name prefix applied to every Workbook so they cluster together in the portal Workbooks gallery. Default "mdk · " — override per-tenant if you want e.g. "Acme · mdk · ".')
param namePrefix string = 'mdk · '

@description('Common tags applied to every resource (carry the deployment + cost-center tags forward from main.bicep).')
param tags object = {}

// ---------------------------------------------------------------------------
// The four persona Workbooks. Each wraps one JSON under
// infra/azure-monitor/workbooks/ (two dirs up from infra/azure/modules/).
// `category: 'workbook'` is the gallery category; `version: '1.0'` is the
// Workbook content version (distinct from the api-version).
// ---------------------------------------------------------------------------

resource operatorWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'mdk-operator-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: '${namePrefix}operator'
    serializedData: loadTextContent('../../azure-monitor/workbooks/operator.workbook.json')
    sourceId: workspaceResourceId
    category: 'workbook'
    version: '1.0'
  }
}

resource platformWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'mdk-platform-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: '${namePrefix}platform'
    serializedData: loadTextContent('../../azure-monitor/workbooks/platform.workbook.json')
    sourceId: workspaceResourceId
    category: 'workbook'
    version: '1.0'
  }
}

resource evalAndDriftWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'mdk-eval-and-drift-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: '${namePrefix}eval & drift'
    serializedData: loadTextContent('../../azure-monitor/workbooks/eval-and-drift.workbook.json')
    sourceId: workspaceResourceId
    category: 'workbook'
    version: '1.0'
  }
}

resource tenantOpsWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'mdk-tenant-ops-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: '${namePrefix}tenant ops'
    serializedData: loadTextContent('../../azure-monitor/workbooks/tenant-ops.workbook.json')
    sourceId: workspaceResourceId
    category: 'workbook'
    version: '1.0'
  }
}

// Temporal operational workbook (ADR 082) — durable-workflow throughput +
// success/failure rate from the mdk.workflow.completed counter the Temporal
// terminal activity emits. Rides the SAME enableWorkbooks gate; only meaningful
// once the self-hosted Temporal backend (ADR 078) is deployed + emitting, but
// the workbook is harmless (empty panels) otherwise.
resource temporalWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'mdk-temporal-workbook')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: '${namePrefix}temporal'
    serializedData: loadTextContent('../../azure-monitor/workbooks/temporal.workbook.json')
    sourceId: workspaceResourceId
    category: 'workbook'
    version: '1.0'
  }
}

// ---------------------------------------------------------------------------
// Outputs — resource ids so main.bicep / downstream tooling can reference the
// deployed Workbooks (e.g. to print a portal link after deploy).
// ---------------------------------------------------------------------------

@description('Resource id of the deployed operator Workbook.')
output operatorWorkbookId string = operatorWorkbook.id

@description('Resource id of the deployed platform Workbook.')
output platformWorkbookId string = platformWorkbook.id

@description('Resource id of the deployed eval-and-drift Workbook.')
output evalAndDriftWorkbookId string = evalAndDriftWorkbook.id

@description('Resource id of the deployed tenant-ops Workbook.')
output tenantOpsWorkbookId string = tenantOpsWorkbook.id

@description('Resource id of the deployed temporal Workbook (ADR 082).')
output temporalWorkbookId string = temporalWorkbook.id
