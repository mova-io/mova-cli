// Log Analytics workspace — base for ACA + ACR + Postgres diagnostics.
// Provisioned first because the Container Apps Environment binds to it
// at creation; reordering would require a destroy-recreate.

@description('Name of the workspace.')
param name string

@description('Azure region for the workspace.')
param location string

@description('Retention in days. dev/staging=30, prod=90 per docs/v1.0-azure-design.')
@minValue(7)
@maxValue(730)
param retentionInDays int = 30

@description('Common tags applied to every resource.')
param tags object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      // PerGB2018 is the modern default; Free tier is deprecated.
      // Cost: ~$2.30/GB ingested + first 5 GB/mo free.
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    features: {
      // Disable automatic data tier changes — predictable cost beats
      // marginal savings.
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

@description('Workspace resource id; ACA env consumes this at creation.')
output workspaceId string = workspace.id

@description('Customer id (a.k.a. workspace id) — used by some agents.')
output customerId string = workspace.properties.customerId

@description('Resource name (echoed for output composition).')
output workspaceName string = workspace.name
