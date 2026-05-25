// OpenTelemetry Collector Container App — the in-cluster bridge that lets the
// movate runtime stay generic-OTLP (ADR 001) while traces still land in
// Application Insights.
//
// WHY THIS EXISTS (a proven live-Azure finding — see ADR 020):
//   The Container Apps Environment's *managed* OpenTelemetry only supports
//   `dataDogConfiguration` + `otlpConfigurations` destinations. The
//   `appInsightsConfiguration` destination we previously configured on the CAE
//   is NOT in the RP's type defs (hence BCP037) and is rejected at deploy time
//   with the misleading preflight error
//   "AppInsightsConfiguration.ConnectionString can not be empty" — even with a
//   valid 240-char connection string. `az bicep build` and
//   `az deployment group validate` both PASS the broken config; only a real
//   `az deployment group create` exposes it (six real attempts failed).
//
// THE DESIGN:
//   Run the OpenTelemetry Collector **contrib** image (the only distro that
//   ships the `azuremonitor` exporter) as an internal-ingress Container App.
//   The api/worker emit generic OTLP/HTTP to it; the collector's
//   `azuremonitor` exporter forwards traces/metrics/logs to App Insights.
//
//   Data flow:  api/worker (OTLP/HTTP) → otel-collector (azuremonitor) → App Insights
//
// Image note: otel/opentelemetry-collector-contrib is a public Docker Hub
// image, so — like the langfuse module — there is intentionally no
// `registries` block (ACA pulls public images without a credential) and no
// `identity` (no KV read, no ACR pull: the App Insights connection string
// arrives as a plain param, not a KV secretRef).

@description('Container App name.')
param name string

@description('Azure region.')
param location string

@description('Container Apps Environment id.')
param environmentId string

@description('''
Application Insights connection string the collector's azuremonitor exporter
ships telemetry to. Passed as a PLAIN (not @secure()) param on purpose:
  - ARM omits @secure() params during preflight, which is exactly when the
    value must be present — that was a contributor to the managed-OTel
    "ConnectionString can not be empty" failure.
  - It carries a write-only ingestion key (low sensitivity) and is never
    surfaced as a deployment output.
The collector reads it at runtime via ${env:APPLICATIONINSIGHTS_CONNECTION_STRING}.
''')
param appInsightsConnectionString string

@description('''
Resource id of a user-assigned managed identity. Accepted for parity with the
other app modules but UNUSED here — the collector pulls a public image and
reads no Key Vault secrets, so it needs no identity. Empty string (default)
leaves the Container App with no `identity` block at all.
''')
param userAssignedIdentityId string = ''

@description('Min replicas. One collector replica is plenty for a single-team trace volume.')
@minValue(0)
@maxValue(5)
param minReplicas int = 1

@description('Max replicas.')
@minValue(1)
@maxValue(10)
param maxReplicas int = 1

@description('CPU per replica.')
param cpu string = '0.5'

@description('Memory per replica.')
param memory string = '1.0Gi'

@description('Common tags.')
param tags object = {}

// OTLP/HTTP receiver port. ACA internal ingress serves on :443 and forwards to
// this targetPort, so callers use https://<fqdn> (no port) — the OTLP/HTTP
// exporter appends /v1/traces itself.
var otlpHttpPort = 4318

// Collector config, supplied to the container via the OTELCOL_CONFIG env var
// (the `--config=env:OTELCOL_CONFIG` command below reads it from there — no
// file mount needed). The collector expands `${env:NAME}` references in its
// config at runtime, so the azuremonitor exporter picks up the connection
// string from the APPLICATIONINSIGHTS_CONNECTION_STRING env var.
//
// BICEP ESCAPING GOTCHA (the #1 risk of this module):
//   In a SINGLE-quoted Bicep string a literal `${...}` is string
//   interpolation, so `${env:APPLICATIONINSIGHTS_CONNECTION_STRING}` would
//   compile to an EMPTY string (no such Bicep symbol) and you'd need the
//   `${'$'}{env:NAME}` escape. This is a MULTI-LINE (triple-quote `'''...'''`)
//   string, which is VERBATIM/RAW: Bicep performs NO interpolation and NO
//   escaping inside it. So `${env:...}` is already a literal here — the plain
//   form is correct, and the `${'$'}` escape would WRONGLY leak the literal
//   characters `${'$'}` into the deployed value. Confirmed: the compiled ARM
//   carries the literal `${env:APPLICATIONINSIGHTS_CONNECTION_STRING}`, which
//   the collector's env: config provider then expands at runtime.
var otelCollectorConfig = '''
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
      grpc:
        endpoint: 0.0.0.0:4317
exporters:
  azuremonitor:
    connection_string: ${env:APPLICATIONINSIGHTS_CONNECTION_STRING}
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [azuremonitor]
    metrics:
      receivers: [otlp]
      exporters: [azuremonitor]
    logs:
      receivers: [otlp]
      exporters: [azuremonitor]
'''

resource collector 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  // Only attach an identity when one is supplied. The collector needs none
  // (public image, connection string is a plain param), so the default empty
  // string leaves the app with no `identity` block.
  identity: empty(userAssignedIdentityId) ? null : {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      ingress: {
        // Internal only — the collector is reachable from the api/worker
        // inside the Container Apps Environment, never from the public
        // internet. (external:false → an *.internal.<defaultDomain> FQDN.)
        external: false
        targetPort: otlpHttpPort
        // OTLP/HTTP. ACA terminates TLS at the internal ingress (:443) and
        // forwards plaintext to targetPort 4318.
        transport: 'http'
        allowInsecure: false
      }
      // No `registries` — otel/opentelemetry-collector-contrib is a public
      // Docker Hub image, pulled without a credential (cf. the langfuse module).
    }
    template: {
      containers: [
        {
          name: 'otel-collector'
          // contrib distro: the only one bundling the `azuremonitor` exporter.
          // Pinned to a recent stable tag for reproducible deploys.
          image: 'otel/opentelemetry-collector-contrib:0.115.1'
          // Read the pipeline config from the OTELCOL_CONFIG env var below
          // (env: provider) — avoids mounting a config file into the container.
          // NOTE: this MUST be `args`, not `command`. In ACA, `command`
          // overrides the image ENTRYPOINT (it would try to exec the flag as a
          // binary → "executable file not found"); `args` are passed to the
          // image's default entrypoint (/otelcol-contrib). Mirrors the api
          // module's command:['movate'] + args:['serve',...] split.
          args: [
            '--config=env:OTELCOL_CONFIG'
          ]
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              // The collector YAML (above). The collector's env: config
              // provider reads this, then expands the ${env:...} reference
              // inside it against APPLICATIONINSIGHTS_CONNECTION_STRING.
              name: 'OTELCOL_CONFIG'
              value: otelCollectorConfig
            }
            {
              // Consumed by the azuremonitor exporter via the
              // ${env:APPLICATIONINSIGHTS_CONNECTION_STRING} reference in the
              // config. Plain value (write-only ingestion key) — see the param
              // doc for why it is intentionally not a @secure() / KV secretRef.
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

@description('Internal ingress FQDN of the collector (e.g. <name>.internal.<env-default-domain>). The api/worker set OTEL_EXPORTER_OTLP_ENDPOINT to https://<this>.')
output fqdn string = collector.properties.configuration.ingress.fqdn

@description('Collector Container App name.')
output name string = collector.name
