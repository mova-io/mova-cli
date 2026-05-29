// ADR 039 Phase 1 — subscription-scoped role assignment for Movate's
// Managed Grafana managed identity. Granted `Monitoring Reader` (read of
// telemetry only) on Movate's OWN subscription, symmetric to what the
// Lighthouse offer delegates into each customer's RG.
//
// Invoked as a nested module from `managed-grafana.bicep` so the parent can
// stay resource-group-scoped. Do not deploy this file directly.

targetScope = 'subscription'

@description('Object ID of Grafana\'s system-assigned managed identity.')
param principalId string

@description('Stable GUID for the role assignment name (idempotent across reruns).')
param roleAssignmentName string

@description('Built-in role definition GUID (Monitoring Reader = 43d0d8ad-25c7-4714-9337-8ba259a9fe05).')
param roleDefinitionId string

resource miMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: roleAssignmentName
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionId)
  }
}
