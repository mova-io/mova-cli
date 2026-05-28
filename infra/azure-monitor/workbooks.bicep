// Azure Monitor Workbooks — Bicep wrapper around the prescriptive Workbook
// JSONs that ship under infra/azure-monitor/workbooks/<name>.workbook.json.
//
// STATUS: DRAFT — waiting on the four Workbook JSONs to land. PR #518
// (docs/observability-demo-assets) ships three GRAFANA dashboards + ONE Azure
// Workbook (dashboards/azure/mdk-golden-signals.workbook.json) — NOT the four
// prescriptive Workbooks (operator, platform, eval-and-drift, tenant-ops) this
// module wraps. Once the follow-up PR lands those JSONs at
// infra/azure-monitor/workbooks/*.workbook.json, uncomment the four resource
// blocks below; the rest of the wiring (params, idempotent names, outputs) is
// already in place and validates today.
//
// CANONICAL PATTERN — Workbooks-as-code in Azure:
//   * loadTextContent() inlines the JSON at template-compile time. Edit the
//     JSON, re-run `az deployment group create`, and the deployed Workbook is
//     refreshed in place. No re-encoding the JSON inside Bicep, no drift.
//   * `name` is derived via guid(resourceGroup().id, '<stable-key>') so
//     re-deploys are idempotent — the same RG + key always resolves to the
//     same workbook resource.
//   * `kind: 'shared'` makes the Workbook visible to anyone with read on the
//     RG (vs `kind: 'user'`, which scopes it to a single principal).
//   * `sourceId` is the Log Analytics workspace the Workbook's KQL targets
//     (workspace-based App Insights writes to App* tables in that workspace —
//     see infra/azure/modules/monitor-alerts.bicep for the same convention).
//
// API VERSION: 2023-06-01 — current GA at time of writing. Matches the pattern
// used elsewhere in infra/ (Microsoft.Insights resources pinned at
// 2023-* GA api-versions; see monitor-alerts.bicep / appinsights.bicep).

@description('Resource id of the EXISTING Log Analytics workspace the Workbooks query (i.e. the workspace your workspace-based App Insights writes to). Same shape as `workspaceResourceId` in infra/azure/modules/monitor-alerts.bicep.')
param logAnalyticsWorkspaceId string

@description('Azure region for the Workbook resources. Defaults to the RG location.')
param location string = resourceGroup().location

@description('Display-name prefix applied to every Workbook so they cluster together in the portal Workbooks gallery. Default "MDK · " — override per-tenant if you want e.g. "Acme · MDK · ".')
param namePrefix string = 'MDK · '

@description('Common tags applied to every resource (carry the deployment + cost-center tags forward from main.bicep).')
param tags object = {}

// ---------------------------------------------------------------------------
// PRESCRIPTIVE WORKBOOKS — wrapper resources, one per JSON.
//
// These four resource blocks are commented out pending the follow-up PR that
// produces the JSONs they reference. When the JSONs land at the paths shown
// in each loadTextContent() call, uncomment the block and re-run
// `az bicep build -f infra/azure-monitor/workbooks.bicep` to verify. The
// resource shape, name derivation, sourceId wiring, and outputs are settled —
// only the JSON files are missing.
//
// Each Workbook's role:
//   * operator         — on-call shift view: golden signals, dead-letter spike,
//                        latency p95, traffic, recent alerts.
//   * platform         — platform-engineering view: resource health, infra
//                        capacity, cost trend, deploy timeline.
//   * eval-and-drift   — eval-bench + drift dashboard: pass-rate over time,
//                        per-judge breakdown, drift alerts firing.
//   * tenant-ops       — per-tenant slice: requests / errors / latency
//                        filterable by tenant tag.
// ---------------------------------------------------------------------------

// resource operatorWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
//   name: guid(resourceGroup().id, 'mdk-operator-workbook')
//   location: location
//   tags: tags
//   kind: 'shared'
//   properties: {
//     displayName: '${namePrefix}Operator'
//     serializedData: loadTextContent('workbooks/operator.workbook.json')
//     sourceId: logAnalyticsWorkspaceId
//     category: 'workbook'
//     version: '1.0'
//   }
// }
//
// resource platformWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
//   name: guid(resourceGroup().id, 'mdk-platform-workbook')
//   location: location
//   tags: tags
//   kind: 'shared'
//   properties: {
//     displayName: '${namePrefix}Platform'
//     serializedData: loadTextContent('workbooks/platform.workbook.json')
//     sourceId: logAnalyticsWorkspaceId
//     category: 'workbook'
//     version: '1.0'
//   }
// }
//
// resource evalAndDriftWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
//   name: guid(resourceGroup().id, 'mdk-eval-and-drift-workbook')
//   location: location
//   tags: tags
//   kind: 'shared'
//   properties: {
//     displayName: '${namePrefix}Eval & Drift'
//     serializedData: loadTextContent('workbooks/eval-and-drift.workbook.json')
//     sourceId: logAnalyticsWorkspaceId
//     category: 'workbook'
//     version: '1.0'
//   }
// }
//
// resource tenantOpsWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
//   name: guid(resourceGroup().id, 'mdk-tenant-ops-workbook')
//   location: location
//   tags: tags
//   kind: 'shared'
//   properties: {
//     displayName: '${namePrefix}Tenant Ops'
//     serializedData: loadTextContent('workbooks/tenant-ops.workbook.json')
//     sourceId: logAnalyticsWorkspaceId
//     category: 'workbook'
//     version: '1.0'
//   }
// }

// ---------------------------------------------------------------------------
// Outputs — surfaced once the resources above are activated. Listed here as
// a comment so downstream consumers can see the contract; un-comment in
// lockstep with the resources above.
// ---------------------------------------------------------------------------

// output operatorWorkbookId string = operatorWorkbook.id
// output platformWorkbookId string = platformWorkbook.id
// output evalAndDriftWorkbookId string = evalAndDriftWorkbook.id
// output tenantOpsWorkbookId string = tenantOpsWorkbook.id

// Until the resources are activated we still expose the workspace id we were
// handed, so the deployment surfaces something useful (mainly: confirms the
// param was wired through end-to-end).
@description('Echo of the Log Analytics workspace id passed in — sanity check that the param round-trips while the workbook resources are pending.')
output logAnalyticsWorkspaceIdEcho string = logAnalyticsWorkspaceId
