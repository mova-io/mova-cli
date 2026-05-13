// movate-teams-bot Container App — runs `movate teams-bot serve` behind external ingress.
//
// Same Docker image as the API + worker; only the command differs.
// Sits alongside `containerapp-api.bicep` because the resource shape is identical;
// keeping them in separate modules makes the param surface and scale rules independent.
//
// Auth model
// ----------
// The bot reads three secrets from Key Vault at startup:
//
//   1. ``MOVATE_TEAMS_FLEET_API_KEY`` — bot's fleet Movate API key for
//      admin ops + the fallback path when a user hasn't run /movate connect.
//   2. ``MOVATE_TEAMS_ENCRYPTION_KEY`` — Fernet key for the per-user
//      identity-binding store (3.1.c). 32-byte url-safe-base64.
//   3. ``MICROSOFT_APP_PASSWORD`` — Bot Service AAD app secret. Used
//      for JWT validation of inbound webhooks (slot ready for the
//      hardening PR; not consumed yet in 3.1.e).
//
// Ingress is external (the Bot Service connector dispatches to a public URL).

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

@description('Image tag, e.g. movate:0.7.0.')
param image string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('Public URL of the Movate runtime the bot forwards to (movate serve).')
param runtimeUrl string

@description('AAD app id for the Bot Service registration. Used by the bot to validate inbound JWTs (hardening PR slot).')
param microsoftAppId string = ''

@description('Min replicas — 1 keeps a warm pool; 0 lets ACA scale to zero between webhook bursts.')
@minValue(0)
@maxValue(10)
param minReplicas int = 1

@description('Max replicas — Teams webhooks are low-volume but bursty during demos.')
@minValue(1)
@maxValue(30)
param maxReplicas int = 3

@description('CPU per replica.')
param cpu string = '0.5'

@description('Memory per replica.')
param memory string = '1.0Gi'

@description('Optional public Langfuse base URL. When set, success cards include a "View trace" button.')
param langfusePublicHost string = ''

@description('Strict mode — set true to make `run` require per-user binding (no fleet fallback).')
param requireBinding bool = false

@description('''
Resource id of the user-assigned managed identity this bot
authenticates as. Pre-created at main.bicep top level so the role
assignments (AcrPull, KV Secrets User) are in place BEFORE the bot's
first revision tries to pull the image / read KV. Breaks the chicken-
and-egg deadlock that system-assigned identities trip on a cold deploy.
''')
param userAssignedIdentityId string

@description('Common tags.')
param tags object = {}

resource bot 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    // User-assigned identity created at main.bicep top level so its
    // ACR-pull + KV-secrets-read role assignments can land BEFORE this
    // app's first revision tries to pull the image / read KV. Avoids
    // the cold-deploy deadlock that system-assigned MIs trip on.
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
        // Default to 3978 — the Bot Framework Emulator's expected
        // port. Production Bot Service will hit ``https://<fqdn>/api/messages``
        // regardless of the internal port, but matching the Emulator
        // makes local-dev → prod parity intuitive.
        targetPort: 3978
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          identity: userAssignedIdentityId
        }
      ]
      secrets: [
        {
          name: 'fleet-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/movate-teams-fleet-api-key'
          identity: userAssignedIdentityId
        }
        {
          name: 'encryption-key'
          keyVaultUrl: '${keyVaultUri}secrets/movate-teams-encryption-key'
          identity: userAssignedIdentityId
        }
        {
          name: 'app-password'
          keyVaultUrl: '${keyVaultUri}secrets/microsoft-app-password'
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'movate-teams-bot'
          image: '${acrLoginServer}/${image}'
          command: ['movate']
          args: [
            'teams-bot'
            'serve'
            '--host'
            '0.0.0.0'
            '--port'
            '3978'
            '--runtime-url'
            runtimeUrl
          ]
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              name: 'MOVATE_RUNTIME_URL'
              value: runtimeUrl
            }
            {
              name: 'MOVATE_TEAMS_FLEET_API_KEY'
              secretRef: 'fleet-api-key'
            }
            {
              name: 'MOVATE_TEAMS_ENCRYPTION_KEY'
              secretRef: 'encryption-key'
            }
            // Bot Service AAD credentials. Used by the JWT-validation
            // hardening PR; passed through now so the secret + env
            // wiring is already in place when that code lands.
            {
              name: 'MICROSOFT_APP_ID'
              value: microsoftAppId
            }
            {
              name: 'MICROSOFT_APP_PASSWORD'
              secretRef: 'app-password'
            }
            {
              name: 'MOVATE_TEAMS_LANGFUSE_PUBLIC_HOST'
              value: langfusePublicHost
            }
            {
              name: 'MOVATE_TEAMS_REQUIRE_BINDING'
              value: requireBinding ? '1' : '0'
            }
            // The bot's own sqlite teams_users db. ACA gives us
            // ephemeral storage per replica; the bot survives restarts
            // because the binding-rebind flow (/movate connect again)
            // is friction-light. Production-grade durability would
            // move teams_users to Postgres — tracked as a follow-up.
            {
              name: 'MOVATE_TEAMS_DB'
              value: '/tmp/teams.db'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 3978
              }
              periodSeconds: 30
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
            // HTTP scale — Bot Framework webhooks are HTTP requests;
            // simple concurrency-based scaling matches the load pattern
            // (no Postgres queue like the worker has).
            name: 'http-scale'
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
}

@description('Public URL of the Teams bot webhook (https://<fqdn>/api/messages).')
output webhookUrl string = 'https://${bot.properties.configuration.ingress.fqdn}/api/messages'

@description('The bot Container App resource id — used by main.bicep to grant the same role assignments the api + worker get.')
output containerAppId string = bot.id

// Note: there is intentionally no principalId output here. With the
// UserAssigned identity model, ``bot.identity.principalId`` is empty —
// the meaningful principalId lives on the UAI resource in main.bicep,
// alongside its role assignments. Consumers should reference the UAI
// directly.

@description('ACR id passthrough — keeps the param wired even though the role grant happens in main.bicep, where the dependency edges are clearer. Mirrors the api + worker modules.')
output acrResourceIdEcho string = acrResourceId
