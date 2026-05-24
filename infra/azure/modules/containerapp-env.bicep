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

// Environment properties. NOTE: this module deliberately carries NO
// `openTelemetryConfiguration`. We previously layered on a managed-OTel
// `appInsightsConfiguration` destination here to export traces to App
// Insights, but that does NOT work on live ACA: the managed-OTel surface only
// supports `dataDogConfiguration` + `otlpConfigurations` destinations (which is
// why `appInsightsConfiguration` triggered BCP037 — it is not in the RP type
// defs), and a real `az deployment group create` rejects it at preflight with
// the misleading error "AppInsightsConfiguration.ConnectionString can not be
// empty" even when handed a valid connection string. `az bicep build` and
// `az deployment group validate` both PASS the broken config — only a real
// create exposes it. App Insights export now goes through an in-cluster
// OpenTelemetry Collector instead (modules/containerapp-otel-collector.bicep);
// see ADR 020. The CAE is back to its baseline shape at the stable
// 2024-03-01 API version (the 2024-10-02-preview bump existed only for the
// now-removed openTelemetryConfiguration).
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
