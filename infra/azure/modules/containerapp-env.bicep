// Container Apps Environment — the shared infrastructure that hosts
// movate-api and movate-worker. Wired to Log Analytics so app logs +
// system events flow to one queryable place.

@description('Environment name.')
param name string

@description('Azure region.')
param location string

@description('Log Analytics customer id (workspace id).')
param logAnalyticsCustomerId string

@description('Log Analytics primary shared key. Pulled from listKeys() in main.bicep.')
@secure()
param logAnalyticsSharedKey string

@description('Common tags.')
param tags object = {}

@description('Whether this env is for prod. Adds a Dedicated workload profile alongside Consumption.')
param isProd bool = false

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
    // Workload profiles let prod mix Consumption (scale-to-zero) with
    // Dedicated (always-warm, predictable latency for the API).
    // Dev/staging stay Consumption-only (cheaper, slower cold starts).
    workloadProfiles: isProd ? [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
      {
        name: 'D4'
        workloadProfileType: 'D4'
        minimumCount: 1
        maximumCount: 3
      }
    ] : [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    // Public ingress — no VNet integration in v1.0. Per-app ingress
    // settings (external vs internal) are configured on the apps.
    zoneRedundant: false
  }
}

output envId string = env.id
output envName string = env.name
@description('Default domain (e.g. happymeadow-1234.eastus2.azurecontainerapps.io). API ingress uses this as the FQDN suffix.')
output defaultDomain string = env.properties.defaultDomain
