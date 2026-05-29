// ADR 039 Phase 1 — Movate-side Managed Grafana instance.
//
// SCOPE: applied by Movate ops in MOVATE'S OWN Azure subscription. NOT
// applied to customer subscriptions. The companion Lighthouse offer
// (`infra/movate-telemetry/lighthouse-offer.json`) is what customers deploy
// to delegate `Monitoring Reader` on their Log Analytics workspace into
// Movate's tenant, where this Grafana then reads via its
// system-assigned managed identity using the
// `grafana-azure-monitor-datasource` plugin.
//
// LEAST PRIVILEGE (ADR 039 D2 Phase 1):
//   - The Grafana instance's system-assigned managed identity is granted
//     ONLY `Monitoring Reader` (43d0d8ad-25c7-4714-9337-8ba259a9fe05) at
//     Movate's own subscription scope (symmetric to what the Lighthouse
//     offer delegates into each customer's RG — read of telemetry only).
//   - Movate engineers are granted `Grafana Admin` (built-in role
//     22926164-76b3-42b3-bc55-97df8dab3e41) scoped to THIS Grafana
//     instance — not subscription-wide.
//   - No `Contributor`. No writes. No data-plane secrets.
//
// IDEMPOTENT: role-assignment names use `guid(...)` over stable inputs so
// reruns converge instead of erroring on duplicate names.
//
// DEPLOY: `az deployment group create -g <rg> -f managed-grafana.bicep \
//           --parameters grafanaName=<name> \
//                        adminPrincipalIds='["<oid1>","<oid2>"]'`

targetScope = 'resourceGroup'

@description('Name of the Azure Managed Grafana instance. Operator-chosen; must be globally unique within the subscription. Used verbatim as `resource.name` so reruns are idempotent.')
param grafanaName string

@description('Azure region for the Grafana instance. Defaults to the resource group region; override only if the operator wants Grafana in a different region from the RG (rare).')
param location string = resourceGroup().location

@description('Azure Managed Grafana SKU. ADR 039 D1 selects `Standard` (SLA + zone redundancy + bundled data-source query quota). `Essential` is documented as not-a-fit for an internal product surface.')
@allowed([
  'Standard'
  'Essential'
])
param skuName string = 'Standard'

@description('Entra ID object IDs (NOT application IDs / NOT UPNs) of Movate engineers who get the `Grafana Admin` role on this instance. Each entry is bound via a `Microsoft.Authorization/roleAssignments` scoped to the Grafana resource.')
param adminPrincipalIds array

@description('Common tags applied to every resource emitted by this module.')
param tags object = {}

// --- Built-in role definition IDs -------------------------------------------
// Stable Azure-wide GUIDs — fine to hardcode (not regional, not tenant-local).

// Monitoring Reader — KQL read on Log Analytics + read on Azure Monitor
// resources. This is the SAME role the Lighthouse offer delegates into each
// customer's RG, so the Movate-side and customer-side telemetry posture is
// symmetric: read of telemetry only.
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'

// Grafana Admin — manage dashboards, folders, data sources, and Grafana
// org-level RBAC on a single Managed Grafana instance. Scoped here to the
// Grafana resource (NOT subscription) — least privilege.
var grafanaAdminRoleId = '22926164-76b3-42b3-bc55-97df8dab3e41'

// --- Managed Grafana instance -----------------------------------------------

resource grafana 'Microsoft.Dashboard/grafana@2023-09-01' = {
  name: grafanaName
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  // System-assigned MI: outputs `principalId` (tenant-scoped Entra
  // service-principal object) and an implicit `applicationId`. The
  // applicationId is what the Lighthouse offer's `authorizations[].principalId`
  // field references on the customer side.
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Public access is fine for the internal product surface — Movate
    // operators reach Grafana over the public endpoint with Entra SSO
    // (restricted to `movate.com` directory per ADR 039 D1). Customers
    // never touch this endpoint.
    publicNetworkAccess: 'Enabled'
    // ADR 039 D5: 90-day retention applies to Movate's own LA workspace
    // (Phase 2 only). Phase 1 reads cross-tenant via Lighthouse, so this
    // Grafana retains nothing — no `grafanaIntegrations` configured here.
    // Operators wire Azure Monitor data sources for each customer
    // subscription post-deploy via the portal (it requires the Lighthouse
    // delegation to already exist, which is the customer-side step).
    zoneRedundancy: 'Enabled'
  }
}

// --- Role assignment: Grafana MI → Monitoring Reader on Movate subscription ---
// NOTE: this module's targetScope is `resourceGroup`, so to grant a
// subscription-scoped role assignment we emit a sibling module that hops to
// subscription scope. We do that via a nested deployment below.

module miSubReader './_assign-mi-monitoring-reader.bicep' = {
  name: 'mi-monitoring-reader-${uniqueString(grafana.id)}'
  scope: subscription()
  params: {
    principalId: grafana.identity.principalId
    roleAssignmentName: guid(subscription().id, grafana.id, 'monitoring-reader')
    roleDefinitionId: monitoringReaderRoleId
  }
}

// --- Role assignments: each admin principal → Grafana Admin on THIS Grafana ---
// `Microsoft.Authorization/roleAssignments` works as a child of the Grafana
// resource by setting `scope: grafana`. The name is a guid over (grafana.id,
// principalId, role) so reruns + repeat principals converge.

resource adminBindings 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in adminPrincipalIds: {
  scope: grafana
  name: guid(grafana.id, principalId, grafanaAdminRoleId)
  properties: {
    principalId: principalId
    // Operators are Entra users, not service principals. `User` is the
    // correct principalType; the API accepts `User`, `Group`, or
    // `ServicePrincipal`. If Movate later wants to grant a group, swap
    // this to a per-entry param.
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', grafanaAdminRoleId)
  }
}]

// --- Outputs ----------------------------------------------------------------

@description('Public Grafana endpoint URL (e.g. https://<name>-<hash>.<region>.grafana.azure.com). Movate operators bookmark this.')
output grafanaEndpoint string = grafana.properties.endpoint

@description('Grafana managed identity OBJECT ID (Entra service-principal objectId). Stamped onto the Lighthouse offer below.')
output managedIdentityPrincipalId string = grafana.identity.principalId

@description('Grafana managed identity APPLICATION ID — surfaced as a SEPARATE output name (matches the operator runbook in `docs/movate-telemetry-onboarding.md`) but for Azure Lighthouse `registrationDefinitions.properties.authorizations[].principalId` the OBJECT ID is the correct, documented value. Both outputs return the same value so the operator can copy whichever name they paste into the per-customer parameter file (`movateApplicationId`).')
output managedIdentityApplicationId string = grafana.identity.principalId

@description('Tenant ID of the Movate Entra directory the MI lives in — the customer\'s Lighthouse offer parameters require this as `movateTenantId`.')
output managedIdentityTenantId string = grafana.identity.tenantId
