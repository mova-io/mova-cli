// main.bicep — top-level orchestrator for movate v1.0 Azure deployment.
//
// Targets a resource group (deployment scope). The resource group must
// already exist; the deploy command creates it if missing.
//
// Per-env deployment:
//   az group create -n movate-dev-rg -l eastus2
//   az deployment group create \
//       -g movate-dev-rg \
//       -f infra/azure/main.bicep \
//       -p infra/azure/main.bicepparam.example
//
// Wired-up flow:
//   loganalytics ─┐
//                 │
//   acr           │
//   keyvault      │
//                 ▼
//                cae (Container Apps Env)
//                 │
//                 ├─► api  (managed identity → ACR pull, KV read)
//                 └─► worker (managed identity → ACR pull, KV read)
//                       │
//                       └─► postgres (public IP + Azure FW rule)

targetScope = 'resourceGroup'

// ---------------------------------------------------------------------------
// Parameters
// ---------------------------------------------------------------------------

@description('Environment name: dev, staging, or prod. Drives SKU + replica defaults.')
@allowed(['dev', 'staging', 'prod'])
param env string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Container image (e.g. movate:0.5.0). Pushed to ACR before this deployment runs.')
param image string

@description('Postgres admin password. Should be a Key Vault reference in the bicepparam file.')
@secure()
param postgresAdminPassword string

@description('Tags applied to every resource.')
param tags object = {
  application: 'movate'
  environment: env
  managedBy: 'bicep'
}

@description('''
Deploy the API + worker Container Apps. Set to ``false`` on the
FIRST pass of a fresh environment — the apps reference Key Vault
secrets that don't exist yet, and ACA validates the secret URLs at
create time. First pass with ``enableApiWorker = false`` provisions
Log Analytics + ACR + KV + Postgres + Container Apps Environment,
operator populates KV secrets, second pass flips the flag to
``true`` and the api/worker land.
''')
param enableApiWorker bool = true

@description('''
Short suffix appended to globally-unique resource names (Key Vault,
ACR, Postgres) to avoid collisions with other Azure tenants that
picked the same "movate" branding. KV names live in a global Azure
namespace (3-24 chars, alphanumeric + hyphens); ACR names too (5-50
chars, alphanumeric only). Recommended: 2-6 lowercase alphanumeric
chars — your org slug, your initials, or a UUID slice. Capped at 6
chars because KV is the tightest constraint: ``movate-staging-kv-``
is 18 chars, leaving exactly 6 for the suffix before hitting KV's
24-char ceiling. Empty string keeps the original short names — only
safe on the FIRST tenant to claim them on Azure.
''')
@maxLength(6)
param nameSuffix string = ''

@description('''
Deploy the Teams bot Container App + Azure Bot Service registration.
Same two-pass story as ``enableApiWorker`` — first pass without
Teams (false), populate KV secrets (movate-teams-fleet-api-key,
movate-teams-encryption-key, microsoft-app-password), then second
pass flips this to true. Independent of ``enableApiWorker`` so an
operator can run runtime-only deploys without Teams.

REQUIRES ``teamsBotAppId`` to be set when true — the Bot Service
resource needs the AAD app id at create time.
''')
param enableTeamsBot bool = false

@description('''
AAD application id for the Teams bot. Created OUTSIDE Bicep via:
    az ad app create --display-name "movate-teams-bot-${env}"
Copy the printed appId here. Required when ``enableTeamsBot`` is
true; ignored otherwise. See docs/teams-deploy.md for the full
ordering — Bot Service creation needs this id BEFORE its own
resource is created.
''')
param teamsBotAppId string = ''

@description('''
Optional public Langfuse base URL surfaced as the "View trace"
button on success cards. Off by default — only set when the URL
is routable for the audience (Movate-internal channels only;
prospects shouldn't see an internal URL).
''')
param teamsLangfusePublicHost string = ''

@description('''
Comma-separated list of origins (no spaces) the API's CORS layer
allows. Becomes ``MDK_CORS_ALLOWED_ORIGINS`` on the API container.
Empty string means "no browser callers configured" — the API still
serves server-to-server traffic just fine; browsers from any origin
will be blocked by their own preflight.

Example for the Friday Mova iO demo:
  corsAllowedOrigins = 'http://localhost:4200,https://mova-io.movate.com'

History: before v0.7 this was set by a post-Bicep
`az containerapp update --set-env-vars` step in
scripts/friday-demo-deploy.sh. Threading it through Bicep makes the
deploy idempotent in one pass — re-applying the same template
preserves the value instead of stomping it.
''')
param corsAllowedOrigins string = ''

@description('''
Mount a shared Azure Files volume at /home/movate/agents on both the
API and worker pods. Required for wizard-created agents to survive
cross-pod — without it a POST /api/v1/agents writes to the API pod's
local disk and the worker pods can't see the bundle.

Set ``false`` (default) for dev (single-replica, pod-local is fine)
and ``true`` for staging + prod where multiple replicas and pods run
concurrently. When enabled, a Storage Account + File Share are created
and wired to the Container Apps Environment via a storages binding.
''')
param useAzureFiles bool = false

// ---------------------------------------------------------------------------
// Per-env defaults — keep in sync with docs/v1.0-azure-design §4
// ---------------------------------------------------------------------------

var isProd = env == 'prod'

var acrSku = isProd ? 'Standard' : 'Basic'
var pgSkuTier = isProd ? 'GeneralPurpose' : 'Burstable'
var pgSkuName = isProd ? 'Standard_D2ds_v5' : 'Standard_B1ms'
var pgStorageGB = isProd ? 64 : 32
var pgBackupDays = isProd ? 14 : 7
var logRetentionDays = isProd ? 90 : 30

var apiMinReplicas = isProd ? 2 : 1
var apiMaxReplicas = isProd ? 10 : 2
var apiCpu = isProd ? '1.0' : '0.5'
var apiMemory = isProd ? '2.0Gi' : '1.0Gi'

var workerMinReplicas = isProd ? 2 : 1
var workerMaxReplicas = isProd ? 20 : 2
var workerCpu = isProd ? '1.0' : '0.5'
var workerMemory = isProd ? '2.0Gi' : '1.0Gi'
// Queue depth per replica triggers a scale-up via the KEDA postgresql
// scaler. Prod scales at 10/replica (more headroom — fewer scale
// events, slightly higher steady-state queue); dev scales aggressively
// at 3/replica so a small queue is enough to spin up the second pod.
var workerQueueDepthPerReplica = isProd ? 10 : 3

// ---------------------------------------------------------------------------
// Resource names — see docs/v1.0-azure-design §2 for the convention.
// ---------------------------------------------------------------------------

// nameSuffix is appended to the globally-unique resource names (KV,
// ACR, Postgres — these live in Azure-wide DNS / API namespaces, so a
// vanilla "movate-dev-kv" can be claimed by any other tenant).
// RG-scoped names (logs, ACA env, ACA apps) don't need it — they only
// have to be unique within the RG.
var sfx = empty(nameSuffix) ? '' : '-${nameSuffix}'
var sfxNoHyphen = empty(nameSuffix) ? '' : nameSuffix
var logName = 'movate-${env}-logs'
var acrName = 'movate${env}acr${sfxNoHyphen}'
var kvName = 'movate-${env}-kv${sfx}'
var pgName = 'movate-${env}-pg${sfx}'
var caeName = 'movate-${env}-cae'
var apiName = 'movate-${env}-api'
var workerName = 'movate-${env}-worker'
// Storage account names: globally unique, 3-24 chars, lowercase alphanumeric.
// Max expansion: 'movate' (6) + 'staging' (7) + 'sa' (2) + sfxNoHyphen (≤6) = 21 ✓
var saName = 'movate${env}sa${sfxNoHyphen}'
var teamsBotName = 'movate-${env}-teams-bot'
var botServiceName = 'movate-${env}-bot'
// User-assigned identities for the api + worker + teams-bot apps.
// Pre-created at this level (before the app modules) so role assignments
// can be granted to their principalIds BEFORE the apps exist. Avoids
// the chicken-and-egg deadlock that system-assigned identities trip on a
// cold deploy: app create waits for revision provisioning, revision
// provisioning needs AcrPull + KV Secrets User, those roles wait for
// the app's MI principalId, which doesn't exist until the app + its
// revision are up. With UAIs, the principalId exists immediately, role
// assignments land first, and the app's first revision comes up clean.
var apiUaiName = 'movate-${env}-api-mi'
var workerUaiName = 'movate-${env}-worker-mi'
var teamsBotUaiName = 'movate-${env}-teams-bot-mi'

// ---------------------------------------------------------------------------
// Modules
// ---------------------------------------------------------------------------

module logs 'modules/loganalytics.bicep' = {
  name: 'logs-${env}'
  params: {
    name: logName
    location: location
    retentionInDays: logRetentionDays
    tags: tags
  }
}

module acr 'modules/acr.bicep' = {
  name: 'acr-${env}'
  params: {
    name: acrName
    location: location
    sku: acrSku
    // Admin user only on dev so engineers can `docker login` for
    // local debugging. Prod uses managed identity exclusively.
    adminUserEnabled: env == 'dev'
    tags: tags
  }
}

module kv 'modules/keyvault.bicep' = {
  name: 'kv-${env}'
  params: {
    name: kvName
    location: location
    tags: tags
  }
}

module pg 'modules/postgres.bicep' = {
  name: 'pg-${env}'
  params: {
    name: pgName
    location: location
    skuTier: pgSkuTier
    skuName: pgSkuName
    storageSizeGB: pgStorageGB
    backupRetentionDays: pgBackupDays
    adminPassword: postgresAdminPassword
    tags: tags
  }
}

module cae 'modules/containerapp-env.bicep' = {
  name: 'cae-${env}'
  params: {
    name: caeName
    location: location
    logAnalyticsCustomerId: logs.outputs.customerId
    logAnalyticsSharedKey: listKeys(
      resourceId('Microsoft.OperationalInsights/workspaces', logName),
      '2023-09-01'
    ).primarySharedKey
    isProd: isProd
    tags: tags
  }
  // No explicit dependsOn — Bicep infers the dependency on `logs`
  // through `logs.outputs.customerId` above. The listKeys() call
  // also creates an implicit edge.
}

// ---------------------------------------------------------------------------
// Azure Files — optional shared agents volume (Item 11).
//
// When useAzureFiles=true, a Storage Account + File Share are created and
// a ``Microsoft.App/managedEnvironments/storages`` binding links the share
// to the CAE under the name 'agents-vol'. The API and worker modules then
// mount 'agents-vol' at /home/movate/agents, giving all pods a shared
// filesystem for wizard-created agent bundles.
//
// The storage binding is a CHILD of the CAE environment resource. Because
// the CAE is managed inside the ``cae`` module, we reference it via an
// ``existing`` resource — Bicep compiles this to a GET on the ARM resource
// rather than creating a second copy.
// ---------------------------------------------------------------------------

module azfiles 'modules/azurefiles.bicep' = if (useAzureFiles && enableApiWorker) {
  name: 'azfiles-${env}'
  params: {
    name: saName
    location: location
    tags: tags
  }
}

// Unconditional existing reference — no-op on the ARM side (just a
// symbolic link for dependency tracking). The CHILD resource
// ``caeAgentsStorage`` below carries the ``if`` condition.
resource caeResource 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: caeName
}

// Link the Azure Files share into the CAE so container apps can reference
// it by the name 'agents-vol' in their volumes[] arrays. The binding
// stores the storage account key (via listKeys) in the CAE — it never
// appears in deployment outputs.
resource caeAgentsStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = if (useAzureFiles && enableApiWorker) {
  parent: caeResource
  name: 'agents-vol'
  properties: {
    azureFile: {
      accountName: saName
      // resourceId() is used instead of azfiles.outputs.storageAccountId
      // because Bicep BCP181 requires listKeys() arguments to be
      // calculable at deployment start — conditional module outputs are not.
      // saName is a plain variable so it satisfies the constraint.
      // dependsOn: [azfiles] below restores the ordering that the module
      // output reference previously provided implicitly.
      accountKey: listKeys(resourceId('Microsoft.Storage/storageAccounts', saName), '2023-01-01').keys[0].value
      shareName: 'movate-agents'
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [azfiles, cae]
}

// User-assigned managed identities for the api + worker apps.
// Created UNCONDITIONALLY (even on the infra-only first pass) — they
// cost nothing, they're idempotent across deploys, and pre-staging
// them lets us grant their role assignments early without waiting for
// the apps to exist. See the `apiUaiName` var doc above for the
// deadlock rationale.
resource apiUai 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: apiUaiName
  location: location
  tags: tags
}

resource workerUai 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: workerUaiName
  location: location
  tags: tags
}

// Teams bot UAI. Same rationale as the api/worker UAIs: pre-create at
// the top level so the bot's ACR-pull + KV-secrets-read role
// assignments can land on pass 1, before the Container App tries to
// pull the image / read KV on its first revision.
resource teamsBotUai 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: teamsBotUaiName
  location: location
  tags: tags
}

// Both Container Apps are gated on ``enableApiWorker`` so a fresh
// deployment can run "infra-only" first, the operator populates KV
// secrets, then the second pass deploys the apps. See param doc above.
module api 'modules/containerapp-api.bicep' = if (enableApiWorker) {
  name: 'api-${env}'
  params: {
    name: apiName
    location: location
    environmentId: cae.outputs.envId
    acrLoginServer: acr.outputs.loginServer
    acrResourceId: acr.outputs.registryId
    image: image
    keyVaultUri: kv.outputs.vaultUri
    postgresFqdn: pg.outputs.serverFqdn
    postgresDatabase: pg.outputs.databaseName
    postgresAdminUsername: pg.outputs.adminUsername
    minReplicas: apiMinReplicas
    maxReplicas: apiMaxReplicas
    cpu: apiCpu
    memory: apiMemory
    userAssignedIdentityId: apiUai.id
    corsAllowedOrigins: corsAllowedOrigins
    // Pass the CAE storage config name when Azure Files is enabled;
    // empty string means no volume mount (pod-local /app/agents).
    agentsStorageName: useAzureFiles ? 'agents-vol' : ''
    tags: tags
  }
}

module worker 'modules/containerapp-worker.bicep' = if (enableApiWorker) {
  name: 'worker-${env}'
  params: {
    name: workerName
    location: location
    environmentId: cae.outputs.envId
    acrLoginServer: acr.outputs.loginServer
    acrResourceId: acr.outputs.registryId
    image: image
    keyVaultUri: kv.outputs.vaultUri
    postgresFqdn: pg.outputs.serverFqdn
    postgresDatabase: pg.outputs.databaseName
    postgresAdminUsername: pg.outputs.adminUsername
    minReplicas: workerMinReplicas
    maxReplicas: workerMaxReplicas
    cpu: workerCpu
    memory: workerMemory
    queueDepthPerReplica: workerQueueDepthPerReplica
    userAssignedIdentityId: workerUai.id
    agentsStorageName: useAzureFiles ? 'agents-vol' : ''
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Teams bot (slice 3.1.e) — Container App + Azure Bot Service.
//
// Gated on ``enableTeamsBot`` for the same first-pass / second-pass
// reason as the API + worker. The Bot Service resource needs the
// bot's AAD app id at create time, so operators must run
// `az ad app create` BEFORE flipping enableTeamsBot=true.
//
// The Bot Service forwards Teams Activities to the Container App's
// FQDN at /api/messages. We depend on the Container App fqdn output
// for the Bot Service's messagingEndpoint — Bicep wires the
// ordering automatically.
// ---------------------------------------------------------------------------

module teamsBot 'modules/containerapp-teams-bot.bicep' = if (enableTeamsBot) {
  name: 'teams-bot-${env}'
  params: {
    name: teamsBotName
    location: location
    environmentId: cae.outputs.envId
    acrLoginServer: acr.outputs.loginServer
    acrResourceId: acr.outputs.registryId
    image: image
    keyVaultUri: kv.outputs.vaultUri
    // The bot calls the API via the in-cluster Container Apps FQDN.
    // External traffic flows: Teams → Bot Service → Bot's FQDN; the
    // bot then HTTPs to api's FQDN for the actual runtime calls.
    runtimeUrl: enableApiWorker ? 'https://${api!.outputs.fqdn}' : ''
    microsoftAppId: teamsBotAppId
    langfusePublicHost: teamsLangfusePublicHost
    userAssignedIdentityId: teamsBotUai.id
    // Default to non-strict in alpha; flip via parameters file for
    // multi-tenant prod where attribution matters.
    requireBinding: false
    tags: tags
  }
}

module botService 'modules/bot-service.bicep' = if (enableTeamsBot) {
  name: 'bot-svc-${env}'
  params: {
    name: botServiceName
    // Bot Service is a global resource; location stays 'global' regardless
    // of the deployment's chosen region.
    location: 'global'
    // F0 (free, 10k msg/month) for non-prod; S1 for prod.
    sku: isProd ? 'S1' : 'F0'
    botAppId: teamsBotAppId
    messagingEndpoint: enableTeamsBot ? teamsBot!.outputs.webhookUrl : ''
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Role assignments — give the Container Apps' managed identities the
// permissions they need:
//
//   - "AcrPull" on the registry so they can pull the image
//   - "Key Vault Secrets User" on the vault so they can read secrets
//
// These are top-level resources (not in modules) because the role
// assignment scope (acr, kv) and the assignee (api/worker MI) cross
// module boundaries; placing them here keeps the dependency edges
// explicit.
// ---------------------------------------------------------------------------

// AcrPull built-in role definition id.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
// Key Vault Secrets User built-in role definition id.
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource acrResource 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
  // module dependency forces ordering
}

resource kvResource 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: kvName
}

// Role-assignment `name` must be a GUID derivable from inputs known at
// deployment START. We build it from the scope id + the UAI name +
// the role id — all static. The LIVE UAI principalId (available
// IMMEDIATELY because the UAI is a top-level resource, not nested in
// a module that's gated on enableApiWorker) flows into the
// `properties.principalId` field.
//
// Critically: these role assignments are NOT gated on enableApiWorker.
// They reference the UAIs (which always exist) so they can land on
// pass 1 — that's the whole point of the UAI conversion. When pass 2
// flips enableApiWorker=true, the Container Apps come up with their
// MI permissions already granted; the initial revision pulls the
// image + reads KV without waiting on chicken-and-egg.

resource apiAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acrResource
  name: guid(acrResource.id, apiUaiName, acrPullRoleId)
  properties: {
    principalId: apiUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource workerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acrResource
  name: guid(acrResource.id, workerUaiName, acrPullRoleId)
  properties: {
    principalId: workerUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource apiKvRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kvResource
  name: guid(kvResource.id, apiUaiName, kvSecretsUserRoleId)
  properties: {
    principalId: apiUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

resource workerKvRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kvResource
  name: guid(kvResource.id, workerUaiName, kvSecretsUserRoleId)
  properties: {
    principalId: workerUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

// Teams bot role assignments (slice 3.1.e). Mirror the api + worker
// grants — the bot needs ACR-pull + KV-secrets-read for the same
// reasons (image pull + at-startup secret fetch). Un-gated from
// enableTeamsBot (the UAI always exists; assignments are cheap +
// idempotent so they can land on pass 1 before the app comes up).
resource teamsBotAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acrResource
  name: guid(acrResource.id, teamsBotUaiName, acrPullRoleId)
  properties: {
    principalId: teamsBotUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource teamsBotKvRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kvResource
  name: guid(kvResource.id, teamsBotUaiName, kvSecretsUserRoleId)
  properties: {
    principalId: teamsBotUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Public URL of the movate API. Empty string on first-pass deploys (enableApiWorker=false).')
output apiUrl string = enableApiWorker ? 'https://${api!.outputs.fqdn}' : ''

@description('ACR login server (operators push images here).')
output acrLoginServer string = acr.outputs.loginServer

@description('Key Vault URI (operators set secrets here before first deploy).')
output keyVaultUri string = kv.outputs.vaultUri

@description('Postgres FQDN (for direct admin connections / migrations).')
output postgresFqdn string = pg.outputs.serverFqdn

@description('Teams bot webhook URL (https://<fqdn>/api/messages). Empty when enableTeamsBot=false.')
output teamsBotWebhookUrl string = enableTeamsBot ? teamsBot!.outputs.webhookUrl : ''

@description('Bot Service resource id. Empty when enableTeamsBot=false. Used by Teams Admin Center when publishing.')
output botServiceId string = enableTeamsBot ? botService!.outputs.botServiceId : ''
