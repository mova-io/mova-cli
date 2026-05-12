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
Deploy the Azure Communication Services resource for outbound SMS
notifications. Off by default — SMS adds a separate operator workflow
(A2P 10DLC registration with The Campaign Registry, ~2-3 weeks) and
small monthly cost (~$1/mo per toll-free number + per-message).
Flip to ``true`` only when ready. See docs/v1.0-azure-design.md §10.
''')
param enableSms bool = false

@description('''
E.164 phone number used as the SMS "from" address. Operator obtains
this via `az communication phonenumber purchase` AFTER the ACS
resource is created (first pass with enableSms=true creates ACS,
operator purchases the number out-of-band, then sets this param
and redeploys). Empty string disables the env var on the worker
(falls back to ConsoleSmsBackend at runtime). Leave empty on the
provisioning pass.
''')
param acsFromNumber string = ''

@description('''
Wire Telegram operator-alert env vars into the worker. Off by default —
flip to ``true`` when you want personal job-completion pings via a
Telegram bot. Setup: @BotFather creates the bot + token; you grab your
chat_id from the bot's getUpdates URL; paste the token into KV as
``telegram-bot-token`` and the chat_id into ``telegramChatId`` below.
Unlike SMS (per-job opt-in), Telegram is operator-wide: pings on every
terminal job. Designed for the personal dev-loop use case.
''')
param enableTelegram bool = false

@description('''
Telegram chat_id the worker pings on every terminal job. Numeric
string (e.g. ``987654321``). Non-secret; lives alongside ``image`` in
the bicepparam. Empty string disables the env var even when
``enableTelegram`` is true.
''')
param telegramChatId string = ''

@description('''
Short suffix appended to globally-unique resource names (Key Vault,
ACR, Postgres, ACS) to avoid collisions with other Azure tenants that
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
// ACR, Postgres, ACS — all three live in Azure-wide DNS / API
// namespaces, so a vanilla "movate-dev-kv" can be claimed by any
// other tenant). RG-scoped names (logs, ACA env, ACA apps) don't need
// it — they only have to be unique within the RG.
var sfx = empty(nameSuffix) ? '' : '-${nameSuffix}'
var sfxNoHyphen = empty(nameSuffix) ? '' : nameSuffix
var logName = 'movate-${env}-logs'
var acrName = 'movate${env}acr${sfxNoHyphen}'
var kvName = 'movate-${env}-kv${sfx}'
var pgName = 'movate-${env}-pg${sfx}'
var caeName = 'movate-${env}-cae'
var apiName = 'movate-${env}-api'
var workerName = 'movate-${env}-worker'
var acsName = 'movate-${env}-acs${sfx}'
// User-assigned identities for the api + worker apps. Pre-created at
// this level (before the api/worker modules) so role assignments can be
// granted to their principalIds BEFORE the apps exist. Avoids the
// chicken-and-egg deadlock that system-assigned identities trip on a
// cold deploy: app create waits for revision provisioning, revision
// provisioning needs AcrPull + KV Secrets User, those roles wait for
// the app's MI principalId, which doesn't exist until the app + its
// revision are up. With UAIs, the principalId exists immediately, role
// assignments land first, and the app's first revision comes up clean.
var apiUaiName = 'movate-${env}-api-mi'
var workerUaiName = 'movate-${env}-worker-mi'

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

// Azure Communication Services — gated on enableSms (see param doc).
// Independent of the API/worker apps; can be provisioned in pass 1
// alongside everything else. The phone number is bought out-of-band
// after this lands (Bicep can't reliably express the search-purchase
// flow); see modules/communication.bicep for the operator runbook.
module acs 'modules/communication.bicep' = if (enableSms) {
  name: 'acs-${env}'
  params: {
    name: acsName
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
    // SMS env-var wiring. enableSms gates both: when false, the worker
    // gets neither env var and SMS jobs fall through to console logging.
    // When true, MOVATE_ACS_CONNECTION_STRING is a KV reference (operator
    // populates the secret named "acs-connection-string" once) and
    // MOVATE_ACS_FROM_NUMBER is the non-secret E.164 from the bicepparam.
    enableSms: enableSms
    acsFromNumber: acsFromNumber
    // Telegram env-var wiring. Same shape as SMS: enableTelegram gates
    // both env vars; when true the worker reads MOVATE_TELEGRAM_BOT_TOKEN
    // from KV secret ``telegram-bot-token`` (operator pastes once) and
    // MOVATE_TELEGRAM_CHAT_ID from the bicepparam. Designed for personal
    // alerts ("ping me when my job's done"); operator-wide trigger.
    enableTelegram: enableTelegram
    telegramChatId: telegramChatId
    userAssignedIdentityId: workerUai.id
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
