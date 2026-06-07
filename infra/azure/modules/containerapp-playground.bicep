// movate-playground Container App (ADR 053 D1 + D4) — hosts the EXISTING
// Chainlit playground (`mdk playground serve`) as a shareable, SSO-gated URL.
//
// Same Docker image as the API + worker + teams-bot; only the command differs
// (`mdk playground serve` — Chainlit, NOT the FastAPI runtime). Modeled closely
// on `containerapp-api.bicep`: same UAI identity model, same KV secret-ref
// pattern, same registry pull. It sits BESIDE the runtime in the SAME Container
// Apps Environment (ADR 053 D1), reaching the runtime over the env-internal
// network.
//
// Auth model (ADR 053 D4 — the load-bearing decision)
// ---------------------------------------------------
// Ingress is **external** so the FQDN is genuinely shareable, but access is
// gated by ACA built-in authentication ("Easy Auth") fronted by **Entra ID** —
// configured via the `authConfig` child resource below. The Chainlit app writes
// ZERO auth code: Easy Auth terminates the handshake in front of the container,
// and authenticated requests arrive carrying `X-MS-CLIENT-PRINCIPAL` headers.
//
//   - Org SSO + Entra B2B guest testers authenticate at the platform layer.
//   - The Entra **app registration is operator-pre-created** (a runbook step,
//     NOT silent automation) — its client-id is parameterized below and its
//     client secret is read from Key Vault like every other module's secrets.
//   - `unauthenticatedClientAction: RedirectToLoginPage` makes this NEVER a
//     wide-open public URL (ADR 053 R2): every request is bounced to Entra
//     login before it can reach the container and spend BYOK LLM tokens.
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

@description('Image tag, e.g. movate:0.5.0. SAME image as the runtime — only the command differs (`mdk playground serve`).')
param image string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('''
Base URL of the deployed runtime the playground talks to (ADR 053 D3). Set by
main.bicep to the runtime API app's ingress FQDN so the playground targets the
in-env api app. Consumed by the playground's existing `--runtime-url` /
`MDK_PLAYGROUND_RUNTIME_URL` resolution (the multi-runtime target switcher).
''')
param runtimeUrl string

@description('Postgres FQDN — the Chainlit data layer (ADR 053 D3) shares the runtime DB so history + 👍/👎 feedback persist across the shared instance.')
param postgresFqdn string

@description('Postgres database name (the runtime DB; the playground shares it).')
param postgresDatabase string

@description('Postgres admin username.')
param postgresAdminUsername string

@description('''
Entra ID (Azure AD) application **client id** for Easy Auth (ADR 053 D4). The
app registration is **operator-pre-created** (a runbook step — `az ad app
create`, see docs/playground-deploy.md), NEVER silently automated by this
module. Required: the `authConfig` child resource binds Easy Auth to this
client id.
''')
param entraClientId string

@description('''
Tenant id whose Entra directory issues the Easy Auth tokens. Defaults to the
deployment's own tenant (the common case: Movate staff + Entra B2B guests in
the same directory). Override only for a cross-tenant app registration.
''')
param entraTenantId string = subscription().tenantId

@description('''
Min replicas (ADR 053 D7 — cost guardrail). Defaults to 0 (scale-to-zero): an
idle portal costs nothing and spins up on the first authenticated request. Set
1+ to keep it warm for demos where the cold-start matters.
''')
@minValue(0)
@maxValue(10)
param minReplicas int = 0

@description('Max replicas — a chatty UI, low concurrency; 2 is plenty for a shared test portal.')
@minValue(1)
@maxValue(30)
param maxReplicas int = 2

@description('CPU per replica (cores).')
param cpu string = '0.5'

@description('Memory per replica (e.g. 1.0Gi).')
param memory string = '1.0Gi'

@description('Bind/ingress port for the Chainlit UI. Matches the playground default (8765).')
param targetPort int = 8765

@description('''
Resource id of the user-assigned managed identity this app authenticates as.
Pre-created at main.bicep top level so role assignments (AcrPull on ACR,
"Key Vault Secrets User" on KV) land BEFORE the app's first revision tries to
pull the image / read KV — the same chicken-and-egg deadlock break the api +
worker + teams-bot modules use.
''')
param userAssignedIdentityId string

@description('Common tags.')
param tags object = {}

resource playground 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    // User-assigned identity — pre-created at main.bicep top level so its
    // ACR-pull + KV-secrets-read role assignments are in place BEFORE this
    // app's first revision. See userAssignedIdentityId param doc.
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    configuration: {
      ingress: {
        // External (ADR 053 D1) — the FQDN is the shareable URL the ADR is
        // named for. Public INGRESS, not public ACCESS: the authConfig child
        // resource below puts Easy Auth in front of it (ADR 053 D4 / R2).
        external: true
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          // Identity-based pull via the user-assigned MI (AcrPull role lives
          // on the MI in main.bicep) — same as containerapp-api.bicep.
          identity: userAssignedIdentityId
        }
      ]
      // Key Vault secret-refs (ADR 053 D3 / ADR 018) — secrets land in env
      // vars without ever being in the image or deployment outputs.
      secrets: [
        {
          // Postgres admin password for the shared Chainlit data layer.
          name: 'pg-password'
          keyVaultUrl: '${keyVaultUri}secrets/pg-admin-password'
          identity: userAssignedIdentityId
        }
        {
          // The SCOPED runtime bearer the portal carries (ADR 053 D3 / R5):
          // a least-privilege key (run agents + write feedback), NEVER a
          // fleet-admin / bootstrap key. Operator-minted + uploaded to KV as
          // `playground-runtime-key` (see docs/playground-deploy.md).
          name: 'playground-runtime-key'
          keyVaultUrl: '${keyVaultUri}secrets/playground-runtime-key'
          identity: userAssignedIdentityId
        }
        {
          // Entra app-registration client secret for Easy Auth (ADR 053 D4).
          // Operator-pre-created alongside the app registration; uploaded to
          // KV as `playground-entra-client-secret`. Referenced by the
          // authConfig child resource below via this secret name.
          name: 'playground-entra-client-secret'
          keyVaultUrl: '${keyVaultUri}secrets/playground-entra-client-secret'
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'movate-playground'
          image: '${acrLoginServer}/${image}'
          // Chainlit, NOT the FastAPI runtime (ADR 053 D1). `--headless`
          // (no browser auto-open in a container) + `--no-targets` (the
          // hosted instance pins ONE runtime via --runtime-url, rather than
          // a laptop's multi-target chat-profile picker).
          command: ['mdk']
          args: [
            'playground'
            'serve'
            '--host'
            '0.0.0.0'
            '--port'
            string(targetPort)
            '--headless'
            '--no-targets'
            '--runtime-url'
            runtimeUrl
          ]
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              // Runtime URL (ADR 053 D3) — also passed as --runtime-url above;
              // the env var is the playground's documented resolution path
              // (MDK_PLAYGROUND_RUNTIME_URL) and keeps the two in lockstep.
              name: 'MDK_PLAYGROUND_RUNTIME_URL'
              value: runtimeUrl
            }
            {
              // Scoped runtime bearer (ADR 053 D3 / R5) — the playground's
              // `--api-key` reads MDK_PLAYGROUND_API_KEY. Sourced from the KV
              // secret-ref above; never an env literal.
              name: 'MDK_PLAYGROUND_API_KEY'
              secretRef: 'playground-runtime-key'
            }
            {
              // Chainlit data layer → deployed Postgres (ADR 053 D3). The
              // password slot is empty in the URL; PGPASSWORD (below) supplies
              // it — same PGPASSWORD-style auth the api uses for MDK_DB_URL,
              // since ACA can't string-interpolate a secretRef into a value.
              name: 'MDK_PLAYGROUND_THREADS_URL'
              value: 'postgresql://${postgresAdminUsername}:@${postgresFqdn}:5432/${postgresDatabase}?sslmode=require'
            }
            {
              name: 'PGPASSWORD'
              secretRef: 'pg-password'
            }
          ]
          probes: [
            {
              // Chainlit serves a 200 on / once up. Liveness keeps the pod
              // alive; readiness gates traffic. Both hit the ingress port.
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
            // HTTP concurrency scaling — a chatty UI, not a queue. Low
            // concurrency per replica is fine for a shared test portal.
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
// Easy Auth (ADR 053 D4) — ACA built-in authentication fronting the app with
// Entra ID. ZERO app-auth code: this platform-layer config terminates the
// auth handshake in front of the container. Authenticated requests arrive
// carrying X-MS-CLIENT-PRINCIPAL headers (the seam Phase-2 per-tester
// attribution reads).
//
//   - `globalValidation.unauthenticatedClientAction: RedirectToLoginPage`
//     + `redirectToProvider: azureactivedirectory` → every anonymous request
//     is bounced to Entra login BEFORE it reaches the container (ADR 053 R2:
//     never a wide-open public URL).
//   - The Entra app registration is operator-pre-created (entraClientId param)
//     and its client secret is read from the KV-backed app secret
//     `playground-entra-client-secret` via `clientSecretSettingName`.
// ---------------------------------------------------------------------------
resource playgroundAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: playground
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      // The hard guardrail (ADR 053 R2): unauthenticated callers are redirected
      // to the Entra login page, never served the app. Public ingress, gated
      // access.
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          // v2.0 issuer for the operator-pre-created app registration. B2B
          // guests in this directory authenticate with their own identity.
          // environment().authentication.loginEndpoint is the cloud-correct
          // login host (e.g. https://login.microsoftonline.com/ on public
          // Azure) — keeps this portable across sovereign clouds and silences
          // the no-hardcoded-env-urls linter.
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
          clientId: entraClientId
          // Names the app secret (declared in configuration.secrets above,
          // backed by the KV secret-ref) holding the Entra client secret —
          // the value never appears in the template or outputs.
          clientSecretSettingName: 'playground-entra-client-secret'
        }
        validation: {
          allowedAudiences: [
            // Tokens minted for THIS app registration. api://<clientId> is the
            // default Application ID URI; the explicit client id covers the
            // v2.0 `aud` claim.
            'api://${entraClientId}'
            entraClientId
          ]
        }
      }
    }
    login: {
      // Preserve the originally-requested URL across the login round-trip so
      // a shared deep link lands where intended after SSO.
      preserveUrlFragmentsForLogins: true
    }
  }
}

@description('Public (Easy-Auth-gated) URL of the hosted playground — the shareable link.')
output playgroundUrl string = 'https://${playground.properties.configuration.ingress.fqdn}'

@description('Playground Container App resource id.')
output containerAppId string = playground.id

@description('Playground app name.')
output playgroundName string = playground.name

@description('ACR id passthrough — main.bicep needs it alongside the UAI principalId for the AcrPull role assignment. Mirrors the api + worker + teams-bot modules.')
output acrResourceIdEcho string = acrResourceId
// Note: no principalId output — with the UserAssigned identity model
// `playground.identity.principalId` is empty; the meaningful principalId
// lives on the UAI resource in main.bicep, alongside its role assignments.
