// movate-graph Container App (ADR 081) — hosts the EXISTING knowledge-graph
// dashboard (`mdk graph dashboard`) as a shareable, SSO-gated URL.
//
// The graph *query API* already ships inside the runtime `api` app (ADR 046:
// GET /api/v1/projects/{id}/graph, /graph/nodes/*, analytics, growth stream).
// What was missing was a *hosted viewer*. This module runs the same vendored
// sigma.js + analytics dashboard that `mdk graph dashboard` serves on a laptop,
// but headless in a container pointed at the in-env api app — turning a feature
// we already paid for into a customer-facing surface.
//
// Same Docker image as the API + worker + playground; only the command differs
// (`mdk graph dashboard`). Modeled closely on containerapp-playground.bicep:
// same UAI identity model, same KV secret-ref pattern, same registry pull, same
// Easy Auth. It sits BESIDE the runtime in the SAME Container Apps Environment,
// reaching the api over the env-internal network.
//
// Auth model (mirrors ADR 053 D4 — the load-bearing decision)
// -----------------------------------------------------------
// The dashboard exposes knowledge-graph data (entities + relations extracted
// from customer KBs — potentially sensitive). Ingress is **external** so the
// FQDN is shareable, but access is gated by ACA built-in authentication ("Easy
// Auth") fronted by **Entra ID** — configured via the `authConfig` child below.
// The viewer writes ZERO auth code: Easy Auth terminates the handshake in front
// of the container. The Entra app registration is **operator-pre-created** (a
// runbook step), its client-id parameterized and its secret read from Key Vault.
// `unauthenticatedClientAction: RedirectToLoginPage` makes this NEVER a
// wide-open public URL.
//
// The runtime bearer the viewer uses to call the api stays SERVER-SIDE (the
// dashboard's documented proxy model): it is read from a KV-backed app secret
// into MDK_GRAPH_API_KEY and is never sent to the browser. It is a least-
// privilege, READ-scoped key (the graph API only needs `read`).
//
// Public **ingress** ≠ public **access** — access is always behind Easy Auth.

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

@description('Image tag, e.g. movate:0.5.0. SAME image as the runtime — only the command differs (`mdk graph dashboard`).')
param image string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('''
Base URL of the deployed runtime whose graph API the dashboard proxies (ADR 081).
Set by main.bicep to the runtime API app's ingress FQDN so the viewer targets the
in-env api app. Consumed by the dashboard's headless `--runtime-url` /
`MDK_GRAPH_RUNTIME_URL` resolution (added in ADR 081, mirrors the playground's
`--runtime-url` flag).
''')
param runtimeUrl string

@description('''
Initial knowledge-graph project id to load (optional). The dashboard has an
in-UI project switcher, so this only seeds the first view. Empty (default) opens
the switcher with no project preselected. Becomes MDK_GRAPH_PROJECT_ID.
''')
param projectId string = ''

@description('''
Entra ID (Azure AD) application **client id** for Easy Auth (mirrors ADR 053 D4).
The app registration is **operator-pre-created** (a runbook step — `az ad app
create`), NEVER silently automated by this module. Required: the `authConfig`
child resource binds Easy Auth to this client id.
''')
param entraClientId string

@description('''
Tenant id whose Entra directory issues the Easy Auth tokens. Defaults to the
deployment's own tenant (the common case). Override only for a cross-tenant app
registration.
''')
param entraTenantId string = subscription().tenantId

@description('''
Min replicas (cost guardrail). Defaults to 0 (scale-to-zero): an idle viewer
costs nothing and spins up on the first authenticated request. Set 1+ to keep it
warm where cold-start matters.
''')
@minValue(0)
@maxValue(10)
param minReplicas int = 0

@description('Max replicas — a read-only viewer, low concurrency; 2 is plenty.')
@minValue(1)
@maxValue(30)
param maxReplicas int = 2

@description('CPU per replica (cores).')
param cpu string = '0.25'

@description('Memory per replica (e.g. 0.5Gi).')
param memory string = '0.5Gi'

@description('Bind/ingress port for the dashboard. Matches the `mdk graph dashboard` default (8901).')
param targetPort int = 8901

@description('''
Resource id of the user-assigned managed identity this app authenticates as.
Pre-created at main.bicep top level so role assignments (AcrPull on ACR,
"Key Vault Secrets User" on KV) land BEFORE the app's first revision tries to
pull the image / read KV — same chicken-and-egg break the api/worker/playground
modules use.
''')
param userAssignedIdentityId string

@description('Common tags.')
param tags object = {}

// The graph viewer needs only the runtime URL + scoped bearer. The project id
// env var is included only when a seed project is given (an empty env var would
// trip the dashboard's "no --project" warning on every boot).
var baseEnv = [
  {
    // Runtime URL — also passed as --runtime-url below; the env var is the
    // dashboard's documented headless resolution path (MDK_GRAPH_RUNTIME_URL)
    // and keeps the two in lockstep.
    name: 'MDK_GRAPH_RUNTIME_URL'
    value: runtimeUrl
  }
  {
    // Read-scoped runtime bearer — the dashboard's `--api-key` reads
    // MDK_GRAPH_API_KEY. Sourced from the KV secret-ref below; never a literal.
    name: 'MDK_GRAPH_API_KEY'
    secretRef: 'graph-runtime-key'
  }
]
var projectEnv = empty(projectId)
  ? []
  : [
      {
        name: 'MDK_GRAPH_PROJECT_ID'
        value: projectId
      }
    ]

resource graph 'Microsoft.App/containerApps@2024-03-01' = {
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
        // External so the FQDN is shareable. Public INGRESS, not public ACCESS:
        // the authConfig child below puts Entra Easy Auth in front of it.
        external: true
        targetPort: targetPort
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
          // The SCOPED, READ-only runtime bearer the viewer carries server-side
          // to call the graph API. Operator-minted + uploaded to KV as
          // `graph-runtime-key`. The graph API only needs the `read` scope.
          name: 'graph-runtime-key'
          keyVaultUrl: '${keyVaultUri}secrets/graph-runtime-key'
          identity: userAssignedIdentityId
        }
        {
          // Entra app-registration client secret for Easy Auth. Operator-
          // pre-created alongside the app registration; uploaded to KV as
          // `graph-entra-client-secret`. Referenced by the authConfig child.
          name: 'graph-entra-client-secret'
          keyVaultUrl: '${keyVaultUri}secrets/graph-entra-client-secret'
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'movate-graph'
          image: '${acrLoginServer}/${image}'
          // The vendored sigma.js dashboard, headless: --host 0.0.0.0 binds for
          // ACA; --no-open skips the browser launcher in a container. The
          // runtime URL + read-scoped bearer + seed project arrive via the
          // MDK_GRAPH_* env vars (ADR 081 headless mode), so no ~/.movate
          // config is needed.
          command: ['mdk']
          args: [
            'graph'
            'dashboard'
            '--host'
            '0.0.0.0'
            '--port'
            string(targetPort)
            '--no-open'
          ]
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: concat(baseEnv, projectEnv)
          probes: [
            {
              // The dashboard serves a 200 on / once up.
              type: 'Liveness'
              httpGet: {
                path: '/'
                port: targetPort
              }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/'
                port: targetPort
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
}

// ---------------------------------------------------------------------------
// Easy Auth (mirrors ADR 053 D4) — ACA built-in authentication fronting the app
// with Entra ID. ZERO app-auth code: this platform-layer config terminates the
// auth handshake in front of the container.
//   - unauthenticatedClientAction: RedirectToLoginPage + redirectToProvider:
//     azureactivedirectory → every anonymous request is bounced to Entra login
//     BEFORE it reaches the container (never a wide-open public URL).
//   - The Entra app registration is operator-pre-created (entraClientId) and its
//     client secret is read from the KV-backed app secret
//     `graph-entra-client-secret` via clientSecretSettingName.
// ---------------------------------------------------------------------------
resource graphAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: graph
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          // environment().authentication.loginEndpoint is the cloud-correct
          // login host — keeps this portable across sovereign clouds and
          // silences the no-hardcoded-env-urls linter.
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
          clientId: entraClientId
          clientSecretSettingName: 'graph-entra-client-secret'
        }
        validation: {
          allowedAudiences: [
            'api://${entraClientId}'
            entraClientId
          ]
        }
      }
    }
    login: {
      preserveUrlFragmentsForLogins: true
    }
  }
}

@description('Public (Easy-Auth-gated) URL of the hosted knowledge-graph dashboard — the shareable link.')
output graphUrl string = 'https://${graph.properties.configuration.ingress.fqdn}'

@description('Graph viewer Container App resource id.')
output containerAppId string = graph.id

@description('Graph viewer app name.')
output graphName string = graph.name

@description('ACR id passthrough — main.bicep needs it alongside the UAI principalId for the AcrPull role assignment. Mirrors the api/worker/playground modules.')
output acrResourceIdEcho string = acrResourceId
