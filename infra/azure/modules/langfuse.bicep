// Langfuse v2 self-host — observability backend on Azure, backed by the
// shared Postgres Flexible Server (a dedicated `langfuse` database created
// by postgres.bicep when deployLangfuse=true).
//
// Runs the public ``langfuse/langfuse:2`` image as a Container App with
// external ingress on :3000. Langfuse runs its own Prisma migrations at
// boot, so there's no separate migration step. State lives entirely in
// Postgres, so the app is horizontally stateless.
//
// Secrets (populated in Key Vault by scripts/azure-bootstrap.sh):
//   - langfuse-database-url      full postgres URL incl. password +
//                                ?sslmode=require, pointing at the
//                                `langfuse` database
//   - langfuse-nextauth-secret   openssl rand -base64 32
//   - langfuse-salt              openssl rand -base64 32
//   - langfuse-encryption-key    openssl rand -hex 32  (32-byte hex)
//
// Post-deploy (one-time): open the Langfuse UI at the output ``publicUrl``,
// create an org + project, mint API keys, store them in Key Vault as
// ``langfuse-public-key`` / ``langfuse-secret-key`` (the movate API + worker
// already read those), and set ``LANGFUSE_HOST`` on the movate apps to
// ``publicUrl`` (main.bicep wires this automatically when deployLangfuse=true).
//
// Image note: ``langfuse/langfuse`` is a public Docker Hub image, so there
// is intentionally no ``registries`` block — ACA pulls public images
// without a credential. Only the managed identity (for Key Vault) is needed.

@description('Container App name.')
param name string

@description('Azure region.')
param location string

@description('Container Apps Environment id.')
param environmentId string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('''
Public URL of THIS Langfuse instance — https://<name>.<env-default-domain>.
Used as NEXTAUTH_URL, which MUST match the ingress FQDN or login + API
callbacks break. main.bicep builds this from the CAE default domain.
''')
param publicUrl string

@description('Langfuse image. Pin a 2.x tag (e.g. langfuse/langfuse:2.95.0) for reproducible deploys.')
param image string = 'langfuse/langfuse:2'

@description('''
Resource id of the user-assigned managed identity this app authenticates
as. Pre-created at main.bicep top level + granted "Key Vault Secrets User"
BEFORE this app's first revision tries to read KV — same cold-deploy
deadlock avoidance as the api/worker/teams-bot apps.
''')
param userAssignedIdentityId string

@description('Min replicas. Langfuse is stateless (state in Postgres); 1 keeps the UI warm.')
@minValue(0)
@maxValue(5)
param minReplicas int = 1

@description('Max replicas — the UI + ingestion API are low-volume for a single team.')
@minValue(1)
@maxValue(10)
param maxReplicas int = 2

@description('CPU per replica.')
param cpu string = '0.5'

@description('Memory per replica.')
param memory string = '1.0Gi'

@description('Allow self-service email/password signup. Leave true for first-admin setup; flip false once your account exists.')
param disableSignup bool = false

@description('Common tags.')
param tags object = {}

resource langfuse 'Microsoft.App/containerApps@2024-03-01' = {
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
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
      }
      // No `registries` — langfuse/langfuse is a public Docker Hub image.
      secrets: [
        {
          name: 'database-url'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-database-url'
          identity: userAssignedIdentityId
        }
        {
          name: 'nextauth-secret'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-nextauth-secret'
          identity: userAssignedIdentityId
        }
        {
          name: 'salt'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-salt'
          identity: userAssignedIdentityId
        }
        {
          name: 'encryption-key'
          keyVaultUrl: '${keyVaultUri}secrets/langfuse-encryption-key'
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'langfuse'
          image: image
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'NEXTAUTH_SECRET'
              secretRef: 'nextauth-secret'
            }
            {
              name: 'SALT'
              secretRef: 'salt'
            }
            {
              name: 'ENCRYPTION_KEY'
              secretRef: 'encryption-key'
            }
            {
              // Must equal the public ingress origin (no trailing slash).
              name: 'NEXTAUTH_URL'
              value: publicUrl
            }
            {
              // Langfuse runs `prisma migrate deploy` at boot unless this
              // is "true". Keep migrations on so a fresh `langfuse` DB
              // self-initializes on first start.
              name: 'LANGFUSE_AUTO_POSTGRES_MIGRATION_DISABLED'
              value: 'false'
            }
            {
              name: 'AUTH_DISABLE_SIGNUP'
              value: disableSignup ? 'true' : 'false'
            }
            {
              name: 'TELEMETRY_ENABLED'
              value: 'true'
            }
            {
              name: 'HOSTNAME'
              value: '0.0.0.0'
            }
            {
              name: 'PORT'
              value: '3000'
            }
          ]
          probes: [
            {
              // Langfuse exposes an unauthenticated health endpoint.
              // Generous initial delay — the first boot runs DB migrations.
              type: 'Liveness'
              httpGet: {
                path: '/api/public/health'
                port: 3000
              }
              initialDelaySeconds: 30
              periodSeconds: 30
              failureThreshold: 5
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-scale'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
}

@description('Public URL of the Langfuse UI / ingestion API. Set as LANGFUSE_HOST on the movate apps.')
output publicUrl string = 'https://${langfuse.properties.configuration.ingress.fqdn}'

@description('Langfuse Container App resource id.')
output containerAppId string = langfuse.id
