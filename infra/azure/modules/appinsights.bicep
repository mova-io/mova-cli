// Application Insights — workspace-based, linked to the EXISTING Log
// Analytics workspace. The receiving end for the runtime's OpenTelemetry
// traces: the Container Apps Environment's managed-OTel exporter
// (configured in modules/containerapp-env.bicep) ships spans here, and the
// app stays generic-OTLP (ADR 001 — no Azure-specific SDK in the runtime;
// ACA auto-injects OTEL_EXPORTER_OTLP_ENDPOINT into the containers).
//
// Provisioned only when main.bicep's `enableAppInsights` flag is true.
// Mirrors the style of modules/loganalytics.bicep.

@description('Name of the Application Insights component (RG-scoped).')
param name string

@description('Azure region for the component.')
param location string

@description('''
Resource id of the EXISTING Log Analytics workspace this component binds
to. Workspace-based App Insights (the modern, non-deprecated mode) stores
its telemetry in this workspace rather than the classic standalone store.
Passed from logs.outputs.workspaceId in main.bicep — we never create a new
workspace here.
''')
param workspaceResourceId string

@description('Common tags applied to every resource.')
param tags object = {}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: name
  location: location
  tags: tags
  // 'web' is the standard kind for an OTLP/APM telemetry sink; it doesn't
  // imply a web app, just the schema App Insights uses for distributed
  // traces + dependencies.
  kind: 'web'
  properties: {
    Application_Type: 'web'
    // Workspace-based mode — bind to the EXISTING Log Analytics workspace.
    // The classic standalone store (omitting WorkspaceResourceId) is
    // deprecated and rejected for new components in most regions.
    WorkspaceResourceId: workspaceResourceId
    // Route ingestion through the linked workspace (workspace-based).
    IngestionMode: 'LogAnalytics'
  }
}

// NOTE: the connection string carries an ingestion (write-only) key. We
// surface it as an output so main.bicep can hand it to the Container Apps
// Environment's managed-OTel `appInsightsConfiguration`, which takes the
// connection string inline as a property (that is the ACA API's contract).
// main.bicep does NOT re-emit this as a top-level deployment output, so it
// never lands in `az deployment group show` output.
@description('App Insights connection string (carries an ingestion/write key — consumed inline by ACA managed-OTel; not surfaced as a top-level deploy output).')
output connectionString string = appInsights.properties.ConnectionString

@description('Resource id of the component.')
output id string = appInsights.id

@description('Resource name (echoed for output composition).')
output name string = appInsights.name
