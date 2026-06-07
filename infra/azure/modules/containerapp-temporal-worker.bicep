// movate Temporal worker Container App (ADR 080 D1) — runs
// `movate worker --backend temporal`, polling the Temporal task queue and
// executing `runtime: temporal` workflows (the durable analogue of the native
// job-queue worker). No ingress.
//
// WHY a separate Container App (not a flag on the native worker): the native
// worker (`movate worker`) drains the Postgres job queue and scales on a KEDA
// postgresql scaler; this worker polls the *Temporal* service and its load
// lives there (KEDA can't read it the same way). Distinct substrate + scaling
// signal → distinct app, preserving the one-responsibility-per-app pattern the
// rest of the CAE follows (api / worker / scheduler / langfuse / temporal-server).
//
// Reuses the runtime image + the worker UAI (ACR pull + KV read) + the shared
// Postgres/LLM secrets. Connects to the Temporal frontend via TEMPORAL_HOST
// (ADR 054 D5 BYOK) — the self-hosted server (ADR 078) or Temporal Cloud.
//
// SCALING (ADR 080 D1, Tier 2): fixed replica count for v1. Temporal itself
// distributes tasks across worker replicas; a Temporal-metrics-driven KEDA
// scaler (task-queue backlog) is a follow-up. Bump maxReplicas for throughput.

@description('Container App name.')
param name string

@description('Azure region.')
param location string

@description('Container Apps Environment id.')
param environmentId string

@description('ACR login server.')
param acrLoginServer string

@description('Image tag, e.g. movate:0.5.0 (same image as the api/worker).')
param image string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('Postgres FQDN (the worker activities reuse the Executor → StorageProvider).')
param postgresFqdn string

@description('Postgres database name.')
param postgresDatabase string

@description('Postgres admin username.')
param postgresAdminUsername string

@description('''
Temporal frontend host:port (REQUIRED — this worker exists to execute durable
workflows). e.g. movate-dev-temporal.internal.<domain>:7233 (self-hosted, ADR
078) or <ns>.tmprl.cloud:7233 (Temporal Cloud). Read by
_resolve_temporal_connection (ADR 054 D5).
''')
param temporalHost string

@description('Temporal namespace.')
param temporalNamespace string = 'default'

@description('''
Resource id of the user-assigned managed identity this worker authenticates as.
Reuses the native worker UAI in main.bicep — its AcrPull + KV Secrets User roles
already cover the same image + secrets.
''')
param userAssignedIdentityId string

@description('Self-hosted Langfuse host URL (optional; empty = SDK default / Cloud).')
param langfuseHost string = ''

@description('Trace sink (MDK_TRACE_SINK). Empty = unset. "otlp" ships to the OTLP endpoint.')
param traceSink string = ''

@description('OTLP exporter endpoint (the in-cluster collector base URL). Empty when App Insights export is off.')
param otelExporterEndpoint string = ''

@description('Min replicas. Temporal distributes tasks; 1 keeps a worker warm.')
@minValue(0)
@maxValue(30)
param minReplicas int = 1

@description('Max replicas (throughput ceiling; Temporal load-balances across them).')
@minValue(1)
@maxValue(30)
param maxReplicas int = 2

@description('CPU per replica.')
param cpu string = '0.5'

@description('Memory per replica.')
param memory string = '1.0Gi'

@description('Common tags.')
param tags object = {}

resource temporalWorker 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      // No ingress — this worker polls the Temporal task queue, not HTTP.
      registries: [
        {
          server: acrLoginServer
          identity: userAssignedIdentityId
        }
      ]
      // The langfuse-* secrets are gated on empty(langfuseHost): when Langfuse
      // is off those KV secrets don't exist, and referencing them would
      // hard-fail revision provisioning. The pg/provider secrets are always
      // required (the activities reuse the Executor + StorageProvider).
      secrets: concat([
        {
          name: 'pg-password'
          keyVaultUrl: '${keyVaultUri}secrets/pg-admin-password'
          identity: userAssignedIdentityId
        }
        {
          name: 'openai-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/openai-api-key'
          identity: userAssignedIdentityId
        }
        {
          name: 'anthropic-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/anthropic-api-key'
          identity: userAssignedIdentityId
        }
      ], empty(langfuseHost) ? [] : [
        {
          name: 'langfuse-secret-key'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-secret-key'
          identity: userAssignedIdentityId
        }
        {
          name: 'langfuse-public-key'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-public-key'
          identity: userAssignedIdentityId
        }
      ])
    }
    template: {
      containers: [
        {
          name: 'movate-temporal-worker'
          image: '${acrLoginServer}/${image}'
          command: ['movate']
          // The long-lived Temporal worker (ADR 080 D1 / ADR 054). Registers
          // every runtime:temporal workflow + the activity wrappers on the
          // shared task queue and polls until stopped.
          args: ['worker', '--backend', 'temporal']
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: concat([
            {
              name: 'MDK_DB_URL'
              value: 'postgresql://${postgresAdminUsername}:@${postgresFqdn}:5432/${postgresDatabase}?sslmode=require'
            }
            {
              name: 'PGPASSWORD'
              secretRef: 'pg-password'
            }
            {
              name: 'OPENAI_API_KEY'
              secretRef: 'openai-api-key'
            }
            {
              name: 'ANTHROPIC_API_KEY'
              secretRef: 'anthropic-api-key'
            }
            // Required: where the durable engine lives (ADR 054 D5 BYOK).
            {
              name: 'TEMPORAL_HOST'
              value: temporalHost
            }
            {
              name: 'TEMPORAL_NAMESPACE'
              value: temporalNamespace
            }
          ], empty(langfuseHost) ? [] : [
            {
              name: 'LANGFUSE_SECRET_KEY'
              secretRef: 'langfuse-secret-key'
            }
            {
              name: 'LANGFUSE_PUBLIC_KEY'
              secretRef: 'langfuse-public-key'
            }
            {
              name: 'LANGFUSE_HOST'
              value: langfuseHost
            }
          ], empty(traceSink) ? [] : [
            {
              name: 'MDK_TRACE_SINK'
              value: traceSink
            }
          ], empty(otelExporterEndpoint) ? [] : [
            {
              name: 'OTEL_EXPORTER_OTLP_ENDPOINT'
              value: otelExporterEndpoint
            }
          ])
        }
      ]
      // Fixed replica count (ADR 080 D1) — Temporal distributes tasks across
      // replicas; a task-queue-backlog KEDA scaler is a Tier-2 follow-up.
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

@description('Temporal worker Container App resource id.')
output containerAppId string = temporalWorker.id
