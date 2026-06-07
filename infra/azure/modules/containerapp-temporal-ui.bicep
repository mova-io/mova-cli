// Temporal Web UI on Azure Container Apps (ADR 078 D6) — browse workflows,
// histories, task queues, and pending signals instead of grepping container
// logs. Runs the public ``temporalio/ui`` image; connects to the self-hosted
// Temporal frontend's internal gRPC address (:7233).
//
// ⚠ AUTH: the temporalio/ui image has NO built-in authentication and exposes
// all workflow data (inputs, results, histories — potentially the durable-HITL
// payloads from ADR 062/083). It's gated behind `enableTemporalUi` (default off)
// and INTERNAL ingress by default (browse via the CAE / a port-forward — no
// public exposure). For an EXTERNAL (public-FQDN) deploy the UI MUST be fronted
// by auth: set `authEnabled=true` and this module puts Azure Container Apps
// built-in authentication ("Easy Auth") fronted by Entra ID in front of the
// container (mirrors ADR 053 D4 — the graph/playground apps). ZERO auth code in
// the image: the authConfig child terminates the handshake at the platform edge.
//
// Auth is OPTIONAL at the module level (default off) so the common internal-only
// deploy stays a public-image, no-identity, no-secrets app exactly as before.
// When `authEnabled=true` the app gains a user-assigned identity to read the
// Entra client secret from Key Vault (the only secret it needs). main.bicep
// enables auth by default whenever the UI is external (`temporalUiAuth`).

@description('Container App name.')
param name string

@description('Azure region.')
param location string

@description('Container Apps Environment id.')
param environmentId string

@description('Temporal frontend gRPC address the UI connects to (e.g. movate-dev-temporal.internal.<domain>:7233).')
param temporalAddress string

@description('Temporal UI image. Pin a tag (e.g. temporalio/ui:2.34.0) for reproducible deploys.')
param image string = 'temporalio/ui:2.34.0'

@description('''
Expose the UI publicly. Default false = INTERNAL ingress (reachable only within
the Container Apps Environment). When true, main.bicep fronts it with Entra Easy
Auth by default (`temporalUiAuth`) so a public Temporal UI is never an
unauthenticated leak of workflow histories.
''')
param external bool = false

@description('Public origin of this UI (https://<name>.<env-default-domain>) — used for CORS.')
param publicUrl string = ''

@description('''
Front the UI with ACA built-in authentication ("Easy Auth") fronted by Entra ID
(mirrors ADR 053 D4 — the graph/playground apps). Default false = NO auth (only
safe for the INTERNAL-ingress default). main.bicep sets this true automatically
whenever the UI is external, so a public Temporal UI is never unauthenticated.
When true, `entraClientId`, `userAssignedIdentityId`, and `keyVaultUri` are
required (the operator pre-creates the Entra app registration + uploads its
client secret to KV as `temporal-ui-entra-client-secret`).
''')
param authEnabled bool = false

@description('''
Entra ID (Azure AD) application **client id** for Easy Auth. The app
registration is **operator-pre-created** (a runbook step — `az ad app create`),
NEVER automated by this module. Required when `authEnabled` is true; ignored
otherwise.
''')
param entraClientId string = ''

@description('''
Tenant id whose Entra directory issues the Easy Auth tokens. Defaults to the
deployment's own tenant (the common case). Override only for a cross-tenant app
registration.
''')
param entraTenantId string = subscription().tenantId

@description('''
Resource id of the user-assigned managed identity the app uses to read the Entra
client secret from Key Vault. Required when `authEnabled` is true; ignored
otherwise (the internal-only deploy needs no identity). Pre-created in main.bicep
so its "Key Vault Secrets User" grant lands before the app's first revision.
''')
param userAssignedIdentityId string = ''

@description('''
Key Vault URI (https://<vault>.vault.azure.net/) holding the Easy Auth client
secret `temporal-ui-entra-client-secret`. Required when `authEnabled` is true;
ignored otherwise.
''')
param keyVaultUri string = ''

@description('Min replicas (the UI is low-volume; 1 keeps it warm, 0 allows scale-to-zero).')
@minValue(0)
@maxValue(3)
param minReplicas int = 1

@description('Max replicas.')
@minValue(1)
@maxValue(5)
param maxReplicas int = 2

@description('CPU per replica.')
param cpu string = '0.25'

@description('Memory per replica.')
param memory string = '0.5Gi'

@description('Common tags.')
param tags object = {}

resource ui 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  // Identity ONLY when auth is on — it exists solely to read the Entra client
  // secret from Key Vault. The internal-only deploy keeps zero identity (the
  // image is public Docker Hub, no other secrets). `type: 'None'` is the
  // explicit no-identity form.
  identity: authEnabled
    ? {
        type: 'UserAssigned'
        userAssignedIdentities: {
          '${userAssignedIdentityId}': {}
        }
      }
    : {
        type: 'None'
      }
  properties: {
    environmentId: environmentId
    configuration: {
      ingress: {
        external: external
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      // temporalio/ui is a public Docker Hub image — no registries. The only
      // secret is the Entra client secret for Easy Auth, present only when auth
      // is on (the authConfig child references it by `clientSecretSettingName`).
      secrets: authEnabled
        ? [
            {
              name: 'temporal-ui-entra-client-secret'
              keyVaultUrl: '${keyVaultUri}secrets/temporal-ui-entra-client-secret'
              identity: userAssignedIdentityId
            }
          ]
        : []
    }
    template: {
      containers: [
        {
          name: 'temporal-ui'
          image: image
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              // gRPC address of the Temporal frontend (no scheme). The frontend
              // serves plaintext gRPC over the CAE-internal TCP ingress, so no
              // TLS env is needed here.
              name: 'TEMPORAL_ADDRESS'
              value: temporalAddress
            }
            {
              name: 'TEMPORAL_UI_PORT'
              value: '8080'
            }
            {
              // CORS origin for the UI's own API calls (its SPA → its backend).
              name: 'TEMPORAL_CORS_ORIGINS'
              value: empty(publicUrl) ? 'http://localhost:8080' : publicUrl
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/'
                port: 8080
              }
              initialDelaySeconds: 15
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

// ---------------------------------------------------------------------------
// Easy Auth (mirrors ADR 053 D4 / containerapp-graph.bicep) — ACA built-in
// authentication fronting the UI with Entra ID. Only created when authEnabled.
// ZERO app-auth code: this platform-layer config terminates the auth handshake
// in front of the temporalio/ui container, so every anonymous request is bounced
// to Entra login BEFORE it can read a workflow history.
//   - unauthenticatedClientAction: RedirectToLoginPage + redirectToProvider →
//     never a wide-open public URL.
//   - The Entra app registration is operator-pre-created (entraClientId); its
//     client secret is read from the KV-backed app secret
//     `temporal-ui-entra-client-secret` via clientSecretSettingName.
// ---------------------------------------------------------------------------
resource uiAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (authEnabled) {
  parent: ui
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
          // login host — portable across sovereign clouds, silences the
          // no-hardcoded-env-urls linter.
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
          clientId: entraClientId
          clientSecretSettingName: 'temporal-ui-entra-client-secret'
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

@description('URL of the Temporal Web UI (empty when internal-only).')
output url string = external ? 'https://${ui.properties.configuration.ingress.fqdn}' : ''

@description('Whether Easy Auth (Entra ID) fronts the UI. False = unauthenticated (internal-only).')
output authEnabled bool = authEnabled

@description('Temporal UI Container App resource id.')
output containerAppId string = ui.id
