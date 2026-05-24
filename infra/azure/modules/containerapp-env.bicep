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

@description('App Insights connection string for ACA managed-OTel traces. Empty = managed OTel disabled.')
param appInsightsConnectionString string = ''

// Base environment properties — what we've always emitted. The managed-OTel
// block is layered on conditionally below so the empty (default) case stays
// byte-for-byte identical to the pre-OTel template.
var baseProperties = {
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

// Managed OpenTelemetry — route traces + logs to the App Insights component
// (workspace-based) provisioned in main.bicep. When this is configured, ACA
// auto-injects OTEL_EXPORTER_OTLP_ENDPOINT (+ protocol) into every app's
// containers, so the apps need NO endpoint env var of their own — they only
// flip their trace sink to otlp (see the api/worker traceSink param). The
// app stays generic-OTLP (ADR 001); App Insights is just the destination.
//
// Built as a fragment so it's omitted ENTIRELY when no connection string is
// supplied: empty → {} → union() leaves baseProperties untouched (the
// openTelemetryConfiguration key is never emitted, default-off is unchanged).
var otelFragment = empty(appInsightsConnectionString) ? {} : {
  openTelemetryConfiguration: {
    destinationsConfiguration: {
      // appInsightsConfiguration is valid on the live ACA managed-OTel API
      // but not yet in the Bicep type definitions for managedEnvironments
      // (it lists only dataDog/otlp), so the type checker flags BCP037.
      // Suppress that single known-stale warning; the property is correct.
      // See https://aka.ms/bicep-type-issues.
      #disable-next-line BCP037
      appInsightsConfiguration: {
        connectionString: appInsightsConnectionString
      }
    }
    tracesConfiguration: {
      destinations: ['appInsights']
    }
    logsConfiguration: {
      destinations: ['appInsights']
    }
  }
}

// API version bumped 2024-03-01 → 2024-10-02-preview: the stable versions
// (2024-03-01, 2025-01-01) do NOT expose `openTelemetryConfiguration` on
// ManagedEnvironmentProperties at all; the most recent API version that
// carries the managed-OTel surface is the 2024-10-02-preview line. The
// empty-connection-string path compiles to the same shape as before, so
// existing environments are unaffected by the version bump.
resource env 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: name
  location: location
  tags: tags
  properties: union(baseProperties, otelFragment)
}

output envId string = env.id
output envName string = env.name
@description('Default domain (e.g. happymeadow-1234.eastus2.azurecontainerapps.io). API ingress uses this as the FQDN suffix.')
output defaultDomain string = env.properties.defaultDomain
