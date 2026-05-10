// movate-api Container App — runs `movate serve` behind external ingress.
// Same image as the worker; only the command differs.

@description('Container App name.')
param name string

@description('Azure region.')
param location string

@description('Container Apps Environment id.')
param environmentId string

@description('ACR login server (e.g. movatedacr.azurecr.io).')
param acrLoginServer string

@description('ACR resource id (for managed identity role assignment in main.bicep).')
param acrResourceId string

@description('Image tag, e.g. movate:0.5.0.')
param image string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('Postgres FQDN.')
param postgresFqdn string

@description('Postgres database name.')
param postgresDatabase string

@description('Postgres admin username.')
param postgresAdminUsername string

@description('Min replicas — 1 for dev/staging, 2+ for prod (always-warm).')
@minValue(0)
@maxValue(30)
param minReplicas int = 1

@description('Max replicas.')
@minValue(1)
@maxValue(30)
param maxReplicas int = 2

@description('CPU per replica (cores; 0.5 for dev, 1.0+ for prod).')
param cpu string = '0.5'

@description('Memory per replica (e.g. 1.0Gi).')
param memory string = '1.0Gi'

@description('Common tags.')
param tags object = {}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    // System-assigned identity → used to pull from ACR + read KV secrets.
    // Role assignments live in main.bicep so this module stays focused.
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environmentId
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        // HTTP only inside the env; ACA terminates TLS at the edge.
        transport: 'auto'
        // No special CORS in v1.0 — every consumer is server-to-server
        // with bearer tokens. Browser-facing apps are out of scope.
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          // Identity-based pull. Empty username/passwordSecretRef +
          // identity='system' tells ACA to use the system-assigned MI.
          identity: 'system'
        }
      ]
      // Key Vault references — secrets land in env vars without ever
      // being in the image or deployment outputs. Format:
      //   keyVaultUrl: <vault uri> + secrets/<secret name>
      //   identity: 'system' (managed identity reads KV)
      secrets: [
        {
          name: 'pg-password'
          keyVaultUrl: '${keyVaultUri}secrets/pg-admin-password'
          identity: 'system'
        }
        {
          name: 'openai-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/openai-api-key'
          identity: 'system'
        }
        {
          name: 'anthropic-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/anthropic-api-key'
          identity: 'system'
        }
        {
          name: 'langfuse-secret-key'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-secret-key'
          identity: 'system'
        }
        {
          name: 'langfuse-public-key'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-public-key'
          identity: 'system'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'movate-api'
          image: '${acrLoginServer}/${image}'
          command: ['movate']
          args: ['serve', '--host', '0.0.0.0', '--port', '8000']
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              name: 'MOVATE_DB_URL'
              // Constructed from the secret + non-secret components.
              // asyncpg understands the libpq URL format directly.
              value: 'postgresql://${postgresAdminUsername}:@${postgresFqdn}:5432/${postgresDatabase}?sslmode=require'
            }
            // The above URL has the password slot empty intentionally —
            // ACA can't string-interpolate secretRef into a value field.
            // We use a separate env var the runtime joins itself, OR
            // we move to PGPASSWORD-style auth. For v1.0 we ship a
            // PGPASSWORD env that asyncpg picks up automatically.
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
            {
              name: 'LANGFUSE_SECRET_KEY'
              secretRef: 'langfuse-secret-key'
            }
            {
              name: 'LANGFUSE_PUBLIC_KEY'
              secretRef: 'langfuse-public-key'
            }
            {
              name: 'MOVATE_AGENTS_PATH'
              // Image bakes agents under /app/agents. Operators who
              // want pluggable agents would mount a volume — out of
              // scope for v1.0 (single-tenant agent set per deploy).
              value: '/app/agents'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            // HTTP-based scale: scale out when concurrent in-flight
            // requests exceed N per replica. Default ACA value (10) is
            // conservative for an LLM-bound API where each request
            // can take 1-30s. Bump to 20 for prod once we see real
            // concurrency.
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }

  // Reference acrResourceId so Bicep doesn't warn about an unused param.
  // The actual role assignment that grants this app's MI pull rights
  // lives in main.bicep where the dependency edges are clearer.
}

output apiName string = api.name
output principalId string = api.identity.principalId
output fqdn string = api.properties.configuration.ingress.fqdn
output appResourceId string = api.id
@description('ACR id passthrough — main.bicep needs both this and the API principalId together for the role assignment.')
output acrResourceIdEcho string = acrResourceId
