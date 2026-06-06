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

@description('''
Postgres admin password (Key Vault reference in the bicepparam file). REQUIRED on
the first deploy of a new server (and to rotate); LEAVE EMPTY on redeploys of an
existing server so the current password is retained (postgres.bicep omits the
property when empty). Passing a value on every redeploy is the footgun that reset
the password out from under the apps — deploy-temporal.sh passes empty by default.
''')
@secure()
param postgresAdminPassword string = ''

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
Deploy the scheduler Container Apps **Job** (ADR 017 D2) — a cron-triggered
one-shot that runs ``mdk scheduler-tick``, draining eval + generic
agent/workflow schedules into the existing job queue for the worker to
execute. Default-off (purely additive); flip to ``true`` once schedules are
in use. Requires ``enableApiWorker = true`` (it shares the worker's image +
identity and enqueues into the queue the worker drains).
''')
param enableScheduler bool = false

@description('''
Cron expression (5-field, UTC) for the scheduler Job. The tick is
idempotent + only enqueues schedules whose cadence has elapsed, so running
it more often than the finest cadence is safe. Default: every 5 minutes.
''')
param schedulerCron string = '*/5 * * * *'

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
Deploy the hosted Chainlit playground Container App (ADR 053) — a
shareable, Entra-SSO-gated test portal in the SAME Container Apps
Environment as the runtime. Default-off + purely additive (mirrors
``enableTeamsBot`` / ``enableScheduler`` / ``deployLangfuse``): when
false, ZERO playground resources are emitted and the template is
byte-for-byte unchanged.

Same two-pass story as ``enableApiWorker`` — the app references three
Key Vault secrets (``pg-admin-password``, ``playground-runtime-key``,
``playground-entra-client-secret``) that ACA validates at create time,
so populate them first (see docs/playground-deploy.md), then flip this
true.

REQUIRES ``enableApiWorker = true`` (the playground points at the api
app's in-env FQDN) AND ``playgroundEntraClientId`` set (Easy Auth binds
to the operator-pre-created Entra app registration at create time).
''')
param enablePlayground bool = false

@description('''
Entra ID (Azure AD) application **client id** for the playground's Easy
Auth (ADR 053 D4). The app registration is **operator-pre-created**
OUTSIDE Bicep via:
    az ad app create --display-name "movate-playground-${env}" \
        --web-redirect-uris "https://<playground-fqdn>/.auth/login/aad/callback"
Copy the printed appId here. Required when ``enablePlayground`` is true;
ignored otherwise. See docs/playground-deploy.md for the full ordering —
the redirect URI needs the app's FQDN, so the app is deployed once to
learn its FQDN, then the redirect URI is added and the secret minted.
''')
param playgroundEntraClientId string = ''

@description('''
Tenant id whose Entra directory issues the playground's Easy Auth
tokens. Empty (default) → the deployment's own tenant
(``subscription().tenantId``), the common case (Movate staff + Entra B2B
guests in the same directory). Set only for a cross-tenant app
registration.
''')
param playgroundEntraTenantId string = ''

@description('''
Cold-start knob for the playground (ADR 053 D7). Override for the
playground Container App's ``scale.minReplicas``. Leave at the ``-1``
sentinel (default) to keep the module default of ``0`` (scale-to-zero —
an idle portal costs nothing and spins up on the first authenticated
request). Set ``1``+ to keep it warm for demos. Any value ``>= 0``
overrides; ``-1`` means "use the module default".
''')
@minValue(-1)
@maxValue(10)
param playgroundMinReplicas int = -1

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

@description('''
Deploy a self-hosted Langfuse v2 instance (Container App + a `langfuse`
database on the shared Postgres) and point the API + worker tracing at it
via LANGFUSE_HOST. Off by default — when false, tracing uses whatever
LANGFUSE_HOST/keys are configured on the apps (Langfuse Cloud by default).

Requires the langfuse-* Key Vault secrets to be populated first (see
scripts/azure-bootstrap.sh). Two-pass like the apps: deploy infra + set
secrets, then flip this true.
''')
param deployLangfuse bool = false

@description('Langfuse image. Pin a 2.x tag (e.g. langfuse/langfuse:2.95.0) for reproducible deploys.')
param langfuseImage string = 'langfuse/langfuse:2'

@description('''
Deploy a self-hosted Temporal server (ADR 078) — a Container App running
temporalio/auto-setup, internal gRPC on :7233, backed by `temporal` +
`temporal_visibility` databases on the shared Postgres — and point the API +
worker at it via TEMPORAL_HOST. Off by default; when false ZERO Temporal infra
is emitted and the apps are byte-for-byte unchanged (durable workflows stay
opt-in — native is the floor, ADR 065).

REQUIRES enableApiWorker=true (the apps consume TEMPORAL_HOST). Two-pass like
the rest: deploy infra + ensure the pg-admin-password Key Vault secret is set,
then flip this true. NOTE (ADR 078 D6): the single auto-setup container is one
non-HA Temporal cluster pinned at a single replica. Alternative to Temporal
Cloud (point TEMPORAL_HOST at the cloud namespace instead) — both ride the same
BYOK seam (ADR 054 D5).
''')
param enableTemporal bool = false

@description('Temporal auto-setup image (ADR 078). PIN a tag — never :latest — so schema setup is reproducible.')
param temporalImage string = 'temporalio/auto-setup:1.25.2'

@description('''
Provision a workspace-linked Application Insights component and route the
runtime's OpenTelemetry traces to it through an in-cluster OpenTelemetry
Collector. When true AND ``appInsightsConnectionString`` is non-empty:
(1) an App Insights component is created in workspace-based mode against the
EXISTING Log Analytics workspace (no new workspace), (2) an OTel Collector
Container App (modules/containerapp-otel-collector.bicep) is deployed with
internal ingress on :4318 and its `azuremonitor` exporter pointed at the
connection string, and (3) the api + worker get MDK_TRACE_SINK=otlp PLUS
OTEL_EXPORTER_OTLP_ENDPOINT=https://<collector-fqdn> so movate's generic
OtelTracer ships spans to the collector, which forwards them to App Insights.
The app stays portable (ADR 001 — no Azure SDK; generic OTLP only).

WHY THE COLLECTOR (ADR 020): ACA's *managed* OpenTelemetry does NOT support an
App Insights destination on the live RP — `appInsightsConfiguration` is not in
the type defs (BCP037) and a real deploy fails preflight with
"AppInsightsConfiguration.ConnectionString can not be empty" even with a valid
connection string. The in-cluster collector's `azuremonitor` exporter is the
working path. NOTE: the two-pass nature — the connection string only exists
AFTER the App Insights component is created. Pass 1 creates the component (set
enableAppInsights=true, leave appInsightsConnectionString=''): the collector +
endpoint wiring are gated off until a connection string is supplied. Read the
connection string from the created component, set it as
appInsightsConnectionString, and re-deploy (pass 2) to bring the collector up.

Off by default — purely additive (matches deployLangfuse / enableScheduler /
enableTeamsBot). When false (or with an empty connection string), ZERO new env
vars / collector are emitted and the api/worker stay byte-for-byte unchanged.
The scheduler Job is intentionally left untouched (it does not get the OTLP
endpoint; an otlp sink with no endpoint would fail the tick loud, so the
scheduler keeps its existing trace behavior).
''')
param enableAppInsights bool = false

@description('''
Application Insights connection string the OTel Collector's azuremonitor
exporter ships telemetry to. PLAIN (not @secure()) on purpose: ARM omits
@secure() params during preflight (exactly when the value must be present),
the string carries only a write-only ingestion key (low sensitivity), and it
is never surfaced as a deployment output. Empty (default) means "no collector
yet" — the connection string only exists AFTER the App Insights component is
created, so this drives the two-pass flow described on ``enableAppInsights``:
pass 1 creates the component with this empty, pass 2 supplies the value read
back from the component and the collector + OTLP wiring come up.
''')
param appInsightsConnectionString string = ''

@description('''
Provision Azure Monitor golden-signal alert rules (item 27) on top of the
workspace-based App Insights telemetry — a dead-letter spike, a high
agent.execute error rate, a high agent.execute p95 latency, and an
API-availability / no-traffic alert, each wired to an Action Group. These page
operators on RUNTIME regressions and are distinct from the application-level
drift alerts (item 10, which fire via the NotificationDispatcher/webhook).

Gated on BOTH this flag AND ``enableAppInsights`` (the rules query the App*
tables the workspace-based App Insights populates — no data, no point). Off by
default and purely additive (matches deployLangfuse / enableScheduler /
enableTeamsBot): when false, ZERO Action Group / scheduledQueryRules resources
are emitted and the template is byte-for-byte unchanged.
''')
param enableAlerts bool = false

@description('''
Email address the alerts' Action Group notifies. Empty (default) means the
Action Group is still created when ``enableAlerts=true`` (so the rules evaluate
and surface in the portal Alerts blade) but with NO receiver — nobody is paged
until an operator adds one. Set a distribution list / on-call address to get
emails. Ignored entirely when ``enableAlerts=false``.
''')
param alertEmail string = ''

@description('''
Provision the four prescriptive Azure Monitor Workbooks (operator / platform /
eval-and-drift / tenant-ops) — the Azure-native parallel to the in-repo Grafana
dashboards (dashboards/grafana/). They render the SAME OTel catalog the Grafana
dashboards do, via KQL against the workspace-based App Insights App* tables, and
give on-call / platform-eng / eval-owners / tenant-ops each a portal-native
runbook.

Gated on BOTH this flag AND ``enableAppInsights`` (the Workbook KQL queries the
App* tables the workspace-based App Insights populates — no App Insights, no
data to render). Off by default and purely additive (matches deployLangfuse /
enableScheduler / enableTeamsBot / enableAlerts): when false, ZERO
Microsoft.Insights/workbooks resources are emitted and the template is
byte-for-byte unchanged.
''')
param enableWorkbooks bool = false

@description('''
Cold-start knob for the API. Override for the API Container App's
``scale.minReplicas``. Leave at the ``-1`` sentinel (default) to keep
the per-env default (``dev``/``staging`` = 1, ``prod`` = 2) — i.e. no
change to today's scaling behavior.

Set to ``0`` to let the API scale to zero between requests (cheapest;
the first request after idle pays a cold-start, which can time out a
tight ``GET /healthz`` probe). Set to ``1``+ to keep at least one
replica always-warm so QA/prod never pays the cold-start. Any value
``>= 0`` overrides the per-env default; ``-1`` means "use the default".
''')
@minValue(-1)
@maxValue(30)
param apiMinReplicas int = -1

@description('''
Cold-start knob for the worker — same contract as ``apiMinReplicas``.
Leave at ``-1`` (default) to keep the per-env default
(``dev``/``staging`` = 1, ``prod`` = 2). Set ``0`` to allow scale-to-zero
(KEDA spins a replica up on queue depth, paying a cold-start on the
first queued job after idle); set ``1``+ to keep a warm worker. Any
value ``>= 0`` overrides; ``-1`` uses the per-env default.
''')
@minValue(-1)
@maxValue(30)
param workerMinReplicas int = -1

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

// Per-env minReplicas defaults. Used unless the operator overrides via
// the apiMinReplicas / workerMinReplicas params (sentinel -1 = "use
// this default"). Resolved into the effective values below.
var apiMinReplicasDefault = isProd ? 2 : 1
var apiMaxReplicas = isProd ? 10 : 2
var apiCpu = isProd ? '1.0' : '0.5'
var apiMemory = isProd ? '2.0Gi' : '1.0Gi'

var workerMinReplicasDefault = isProd ? 2 : 1
var workerMaxReplicas = isProd ? 20 : 2
var workerCpu = isProd ? '1.0' : '0.5'
var workerMemory = isProd ? '2.0Gi' : '1.0Gi'
// Queue depth per replica triggers a scale-up via the KEDA postgresql
// scaler. Prod scales at 10/replica (more headroom — fewer scale
// events, slightly higher steady-state queue); dev scales aggressively
// at 3/replica so a small queue is enough to spin up the second pod.
var workerQueueDepthPerReplica = isProd ? 10 : 3

// Effective minReplicas: the operator override (apiMinReplicas /
// workerMinReplicas) when set to a real value (>= 0), else the per-env
// default. The -1 sentinel keeps today's behavior unchanged; an
// operator sets 1 to avoid cold-start (QA/prod) or 0 for scale-to-zero.
// max(override, 0) clamps the override to the module's @minValue(0) and
// proves to the type-checker that the branch result is non-negative (the
// >= 0 guard already excludes the sentinel, so max() never alters a real
// value — it only narrows the inferred type away from -1).
var apiMinReplicasEffective = apiMinReplicas >= 0 ? max(apiMinReplicas, 0) : apiMinReplicasDefault
var workerMinReplicasEffective = workerMinReplicas >= 0 ? max(workerMinReplicas, 0) : workerMinReplicasDefault

// Playground minReplicas (ADR 053 D7). Module default is 0 (scale-to-zero);
// the -1 sentinel means "use that default". max(override, 0) clamps to the
// module's @minValue(0) and proves non-negativity to the type-checker (the
// >= 0 guard already excludes -1, so max() never alters a real value).
var playgroundMinReplicasDefault = 0
var playgroundMinReplicasEffective = playgroundMinReplicas >= 0 ? max(playgroundMinReplicas, 0) : playgroundMinReplicasDefault

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
var schedulerName = 'movate-${env}-scheduler'
// Storage account names: globally unique, 3-24 chars, lowercase alphanumeric.
// Max expansion: 'movate' (6) + 'staging' (7) + 'sa' (2) + sfxNoHyphen (≤6) = 21 ✓
var saName = 'movate${env}sa${sfxNoHyphen}'
var teamsBotName = 'movate-${env}-teams-bot'
var botServiceName = 'movate-${env}-bot'
var playgroundName = 'movate-${env}-playground'
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
var playgroundUaiName = 'movate-${env}-playground-mi'
var langfuseName = 'movate-${env}-langfuse'
var langfuseUaiName = 'movate-${env}-langfuse-mi'
// Self-hosted Temporal server (ADR 078) — RG-scoped names; the app is gated
// on enableTemporal, the UAI is created unconditionally (cold-deploy safety).
var temporalName = 'movate-${env}-temporal'
var temporalUaiName = 'movate-${env}-temporal-mi'
// Temporal worker (ADR 080 D1) — the process that executes runtime:temporal
// workflows. Reuses the native worker UAI (same image + secrets).
var temporalWorkerName = 'movate-${env}-temporal-worker'
// RG-scoped — no global-uniqueness suffix needed (App Insights component
// names only have to be unique within the resource group).
var appInsightsName = 'movate-${env}-appi'
// In-cluster OTel Collector Container App (RG-scoped). Receives generic OTLP
// from the api/worker and forwards to App Insights via its azuremonitor
// exporter — see ADR 020 / modules/containerapp-otel-collector.bicep.
var otelCollectorName = 'movate-${env}-otelcol'

// App Insights export is wired (collector + OTLP endpoint on the apps) only
// when BOTH the feature is on AND a connection string is supplied. The
// connection string only exists after the App Insights component is created,
// so this is the two-pass gate: pass 1 creates the component (connStr=''),
// pass 2 supplies the connStr and the collector + endpoint come up. Used by
// the otelCollector module condition and the api/worker trace wiring below.
var appInsightsExportEnabled = enableAppInsights && !empty(appInsightsConnectionString)

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

// Optional Application Insights (workspace-based) — the receiving end for
// the runtime's OTLP traces. Gated on enableAppInsights (default off). Binds
// to the EXISTING Log Analytics workspace via logs.outputs.workspaceId — no
// new workspace. The connection string is threaded into the CAE module
// below so ACA's managed OpenTelemetry exports spans here.
module appInsights 'modules/appinsights.bicep' = if (enableAppInsights) {
  name: 'appi-${env}'
  params: {
    name: appInsightsName
    location: location
    workspaceResourceId: logs.outputs.workspaceId
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
    createLangfuseDatabase: deployLangfuse
    createTemporalDatabases: enableTemporal
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
    // The CAE no longer carries any openTelemetryConfiguration — its managed
    // OpenTelemetry can't export to App Insights on live ACA (ADR 020). App
    // Insights export is handled by the in-cluster OTel Collector below, which
    // the api/worker emit OTLP to. The CAE is back to its baseline shape.
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

// Playground app UAI (ADR 053). Same rationale as the api/worker/teams-bot
// UAIs: pre-create at the top level (unconditionally — cheap + idempotent) so
// its ACR-pull + KV-secrets-read role assignments land on pass 1, before the
// Container App's first revision tries to pull the image / read KV.
resource playgroundUai 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: playgroundUaiName
  location: location
  tags: tags
}

// Langfuse app UAI. Created unconditionally (cheap, idempotent) so its
// KV-secrets-read grant lands before the app's first revision — even
// though the app itself is gated on deployLangfuse.
resource langfuseUai 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: langfuseUaiName
  location: location
  tags: tags
}

// Temporal server UAI (ADR 078). Created unconditionally (cheap, idempotent)
// so its KV-secrets-read grant (the Postgres password) lands before the app's
// first revision — even though the app itself is gated on enableTemporal.
resource temporalUai 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: temporalUaiName
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
    minReplicas: apiMinReplicasEffective
    maxReplicas: apiMaxReplicas
    cpu: apiCpu
    memory: apiMemory
    userAssignedIdentityId: apiUai.id
    corsAllowedOrigins: corsAllowedOrigins
    langfuseHost: deployLangfuse ? langfuse!.outputs.publicUrl : ''
    // Self-hosted Temporal frontend (ADR 078). Empty when enableTemporal=false
    // → no TEMPORAL_* env emitted → selecting runtime:temporal fails loud
    // (ADR 055 D6), never a silent downgrade. The API needs this to signal a
    // durable run's handle from the resume endpoint (ADR 062 D2).
    temporalHost: enableTemporal ? temporal!.outputs.temporalHost : ''
    // 'otlp' + the collector endpoint are gated on the SAME condition
    // (appInsightsExportEnabled) so the otlp sink always has an endpoint to
    // ship to — movate's fail-loud OtelTracer can't raise TraceSinkError.
    // The endpoint is the collector's INTERNAL ingress base URL: ACA internal
    // ingress serves on :443 → targetPort 4318, and the OTLP/HTTP exporter
    // appends /v1/traces itself, so it's just https://<fqdn> (no port).
    traceSink: appInsightsExportEnabled ? 'otlp' : ''
    otelExporterEndpoint: appInsightsExportEnabled ? 'https://${otelCollector!.outputs.fqdn}' : ''
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
    minReplicas: workerMinReplicasEffective
    maxReplicas: workerMaxReplicas
    cpu: workerCpu
    memory: workerMemory
    queueDepthPerReplica: workerQueueDepthPerReplica
    userAssignedIdentityId: workerUai.id
    agentsStorageName: useAzureFiles ? 'agents-vol' : ''
    langfuseHost: deployLangfuse ? langfuse!.outputs.publicUrl : ''
    // Self-hosted Temporal frontend (ADR 078) — empty when off (see api above).
    temporalHost: enableTemporal ? temporal!.outputs.temporalHost : ''
    // 'otlp' + the collector endpoint gated on the SAME condition — see the
    // api module above for why pairing them keeps the fail-loud OtelTracer
    // safe, and why the endpoint is the bare https://<fqdn> (no port).
    traceSink: appInsightsExportEnabled ? 'otlp' : ''
    otelExporterEndpoint: appInsightsExportEnabled ? 'https://${otelCollector!.outputs.fqdn}' : ''
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Scheduler Container Apps Job (ADR 017 D2) — cron → enqueue. Gated on
// ``enableScheduler`` (default off) AND ``enableApiWorker`` (it shares the
// worker UAI + image and enqueues into the queue the worker drains). Runs
// ``mdk scheduler-tick`` on ``schedulerCron``; the tick is idempotent.
// ---------------------------------------------------------------------------
module scheduler 'modules/containerapp-scheduler.bicep' = if (enableScheduler && enableApiWorker) {
  name: 'scheduler-${env}'
  params: {
    name: schedulerName
    location: location
    environmentId: cae.outputs.envId
    acrLoginServer: acr.outputs.loginServer
    acrResourceId: acr.outputs.registryId
    image: image
    keyVaultUri: kv.outputs.vaultUri
    postgresFqdn: pg.outputs.serverFqdn
    postgresDatabase: pg.outputs.databaseName
    postgresAdminUsername: pg.outputs.adminUsername
    cronExpression: schedulerCron
    // Reuse the worker UAI — its AcrPull + KV Secrets User roles already
    // cover the same image + secrets the scheduler needs.
    userAssignedIdentityId: workerUai.id
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Self-hosted Langfuse (observability on Azure). Gated on deployLangfuse.
// Pulls the public langfuse/langfuse image, talks to the `langfuse`
// database on the shared Postgres (created by the pg module when
// deployLangfuse=true), and reads its secrets from Key Vault. NEXTAUTH_URL
// is built from the CAE default domain so it matches the app's own ingress
// FQDN. The api + worker modules pick up its URL as LANGFUSE_HOST above.
// ---------------------------------------------------------------------------
module langfuse 'modules/langfuse.bicep' = if (deployLangfuse) {
  name: 'langfuse-${env}'
  params: {
    name: langfuseName
    location: location
    environmentId: cae.outputs.envId
    keyVaultUri: kv.outputs.vaultUri
    publicUrl: 'https://${langfuseName}.${cae.outputs.defaultDomain}'
    image: langfuseImage
    userAssignedIdentityId: langfuseUai.id
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Self-hosted Temporal server (ADR 078). Gated on enableTemporal AND
// enableApiWorker (the apps consume its TEMPORAL_HOST). Runs the public
// temporalio/auto-setup image on the shared CAE, talks to the `temporal` +
// `temporal_visibility` databases on the shared Postgres (created by the pg
// module when enableTemporal=true), reads the Postgres password from Key Vault,
// and is reachable only by internal gRPC :7233. The api + worker pick up its
// host:port as TEMPORAL_HOST above. Zero app code change — the worker/API
// connect via the existing BYOK seam (ADR 054 D5).
// ---------------------------------------------------------------------------
module temporal 'modules/containerapp-temporal.bicep' = if (enableTemporal && enableApiWorker) {
  name: 'temporal-${env}'
  params: {
    name: temporalName
    location: location
    environmentId: cae.outputs.envId
    keyVaultUri: kv.outputs.vaultUri
    userAssignedIdentityId: temporalUai.id
    postgresFqdn: pg.outputs.serverFqdn
    postgresAdminUsername: pg.outputs.adminUsername
    image: temporalImage
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Temporal worker (ADR 080 D1). Executes runtime:temporal workflows by polling
// the Temporal task queue — without it, durable workflows compile + register but
// never run. Gated on the SAME condition as the server; reuses the native worker
// UAI (same image + AcrPull + KV-read). Connects to the server's internal FQDN
// via TEMPORAL_HOST (the same value threaded into the api/worker above).
// ---------------------------------------------------------------------------
module temporalWorker 'modules/containerapp-temporal-worker.bicep' = if (enableTemporal && enableApiWorker) {
  name: 'temporal-worker-${env}'
  params: {
    name: temporalWorkerName
    location: location
    environmentId: cae.outputs.envId
    acrLoginServer: acr.outputs.loginServer
    image: image
    keyVaultUri: kv.outputs.vaultUri
    postgresFqdn: pg.outputs.serverFqdn
    postgresDatabase: pg.outputs.databaseName
    postgresAdminUsername: pg.outputs.adminUsername
    temporalHost: temporal!.outputs.temporalHost
    userAssignedIdentityId: workerUai.id
    langfuseHost: deployLangfuse ? langfuse!.outputs.publicUrl : ''
    traceSink: appInsightsExportEnabled ? 'otlp' : ''
    otelExporterEndpoint: appInsightsExportEnabled ? 'https://${otelCollector!.outputs.fqdn}' : ''
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// OpenTelemetry Collector (ADR 020) — the in-cluster bridge that exports the
// runtime's generic OTLP traces to Application Insights via the collector's
// `azuremonitor` exporter. Replaces the (unsupported-on-live-ACA) managed-OTel
// App Insights destination we used to put on the CAE.
//
// Data flow:  api/worker (OTLP/HTTP) → otel-collector (azuremonitor) → App Insights
//
// Gated on appInsightsExportEnabled (enableAppInsights AND a non-empty
// connection string). The connection string only exists AFTER the App Insights
// component is created, so this is a two-pass flow: pass 1 creates the
// component (connStr=''), read it back, pass 2 supplies it and the collector
// comes up. No UAI / role assignments needed: it runs the public contrib image
// (no ACR pull) and the connection string arrives as a plain param (no KV).
// ---------------------------------------------------------------------------
module otelCollector 'modules/containerapp-otel-collector.bicep' = if (appInsightsExportEnabled) {
  name: 'otelcol-${env}'
  params: {
    name: otelCollectorName
    location: location
    environmentId: cae.outputs.envId
    appInsightsConnectionString: appInsightsConnectionString
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Azure Monitor golden-signal alert rules (item 27). Gated on BOTH
// ``enableAlerts`` AND ``enableAppInsights`` — the scheduledQueryRules query the
// App* tables (AppDependencies / AppRequests / AppMetrics) that the
// workspace-based App Insights populates via the OTel Collector's azuremonitor
// exporter, so without App Insights there's nothing to alert on. Scoped to the
// EXISTING Log Analytics workspace (logs.outputs.workspaceId) where those tables
// live. Default-off: with enableAlerts=false the module isn't instantiated, so
// no Action Group / rules are emitted.
//
// Note the gate does NOT require appInsightsExportEnabled (the connection
// string) — the component exists after pass 1, and the workspace tables fill as
// soon as the collector ships data. Operators typically flip enableAlerts on the
// same pass-2 deploy that brings the collector up, but the rules are valid the
// moment App Insights exists.
// ---------------------------------------------------------------------------
module alerts 'modules/monitor-alerts.bicep' = if (enableAlerts && enableAppInsights) {
  name: 'alerts-${env}'
  params: {
    workspaceResourceId: logs.outputs.workspaceId
    appInsightsId: appInsights!.outputs.id
    appInsightsName: appInsights!.outputs.name
    location: location
    alertEmail: alertEmail
    tags: tags
  }
}

// ---------------------------------------------------------------------------
// Azure Monitor Workbooks — the Azure-native parallel to the in-repo Grafana
// dashboards (dashboards/grafana/). Gated on BOTH ``enableWorkbooks`` AND
// ``enableAppInsights`` for the same reason as the alerts module: the Workbook
// KQL targets the App* tables (AppDependencies / AppRequests / AppMetrics) the
// workspace-based App Insights populates via the OTel Collector's azuremonitor
// exporter, so without App Insights there's nothing to render. Each Workbook's
// `sourceId` is the EXISTING Log Analytics workspace (logs.outputs.workspaceId)
// where those tables live — the same workspace the alert rules scope to.
// Default-off: with enableWorkbooks=false the module isn't instantiated, so no
// Microsoft.Insights/workbooks resources are emitted.
// ---------------------------------------------------------------------------
module workbooks 'modules/monitor-workbooks.bicep' = if (enableWorkbooks && enableAppInsights) {
  name: 'workbooks-${env}'
  params: {
    workspaceResourceId: logs.outputs.workspaceId
    location: location
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
// Hosted playground (ADR 053 D1 + D4) — the shareable, Entra-SSO-gated test
// portal. Gated on BOTH ``enablePlayground`` AND ``enableApiWorker``: the
// playground points at the api app's in-env FQDN (it has nothing to talk to
// without the runtime), mirroring how the scheduler is gated on
// ``enableScheduler && enableApiWorker``. Default-off + additive: with
// enablePlayground=false the module isn't instantiated, so no playground app /
// authConfig is emitted and the template is byte-for-byte unchanged.
//
// Easy Auth (D4) binds to an operator-pre-created Entra app registration
// (playgroundEntraClientId) inside the module's authConfig child resource; the
// client secret is read from Key Vault like the other modules' secrets. The
// empty playgroundEntraTenantId default resolves to subscription().tenantId
// inside the module.
// ---------------------------------------------------------------------------
module playground 'modules/containerapp-playground.bicep' = if (enablePlayground && enableApiWorker) {
  name: 'playground-${env}'
  params: {
    name: playgroundName
    location: location
    environmentId: cae.outputs.envId
    acrLoginServer: acr.outputs.loginServer
    acrResourceId: acr.outputs.registryId
    image: image
    keyVaultUri: kv.outputs.vaultUri
    // Talk to the api app over the in-env ingress FQDN (same env → reachable).
    runtimeUrl: enableApiWorker ? 'https://${api!.outputs.fqdn}' : ''
    postgresFqdn: pg.outputs.serverFqdn
    postgresDatabase: pg.outputs.databaseName
    postgresAdminUsername: pg.outputs.adminUsername
    entraClientId: playgroundEntraClientId
    // Empty → the module falls back to subscription().tenantId.
    entraTenantId: empty(playgroundEntraTenantId) ? subscription().tenantId : playgroundEntraTenantId
    minReplicas: playgroundMinReplicasEffective
    userAssignedIdentityId: playgroundUai.id
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

// Playground role assignments (ADR 053). Mirror the api/worker/teams-bot
// grants — the playground needs ACR-pull (same image) + KV-secrets-read
// (pg-admin-password, playground-runtime-key, playground-entra-client-secret).
// Un-gated from enablePlayground (the UAI always exists; assignments are cheap
// + idempotent) so they land on pass 1 before the app's first revision.
resource playgroundAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acrResource
  name: guid(acrResource.id, playgroundUaiName, acrPullRoleId)
  properties: {
    principalId: playgroundUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource playgroundKvRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kvResource
  name: guid(kvResource.id, playgroundUaiName, kvSecretsUserRoleId)
  properties: {
    principalId: playgroundUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

// Langfuse needs KV-secrets-read (DB URL + nextauth/salt/encryption-key).
// No AcrPull grant — it runs the public langfuse/langfuse image, not ours.
// Un-gated (the UAI always exists) so it lands on pass 1.
resource langfuseKvRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kvResource
  name: guid(kvResource.id, langfuseUaiName, kvSecretsUserRoleId)
  properties: {
    principalId: langfuseUai.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

// Temporal server needs KV-secrets-read (the Postgres password). No AcrPull
// grant — it runs the public temporalio/auto-setup image, not ours. Un-gated
// (the UAI always exists) so it lands on pass 1 (ADR 078 D4).
resource temporalKvRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kvResource
  name: guid(kvResource.id, temporalUaiName, kvSecretsUserRoleId)
  properties: {
    principalId: temporalUai.properties.principalId
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

@description('Shareable (Entra-SSO-gated) URL of the hosted playground (ADR 053). Empty when enablePlayground=false. Share THIS link with invited testers; access is gated by Easy Auth.')
output playgroundUrl string = (enablePlayground && enableApiWorker) ? playground!.outputs.playgroundUrl : ''

@description('Self-hosted Langfuse URL. Empty when deployLangfuse=false. Open it to create a project + mint keys, then store them in KV as langfuse-public-key / langfuse-secret-key.')
output langfuseUrl string = deployLangfuse ? langfuse!.outputs.publicUrl : ''

@description('App Insights component name. Empty when enableAppInsights=false. The connection string is intentionally NOT output (it carries an ingestion key).')
output appInsightsName string = enableAppInsights ? appInsights!.outputs.name : ''

@description('Internal ingress FQDN of the OTel Collector. Empty unless App Insights export is wired (enableAppInsights + a connection string). Internal-only — the api/worker reach it via OTEL_EXPORTER_OTLP_ENDPOINT.')
output otelCollectorFqdn string = appInsightsExportEnabled ? otelCollector!.outputs.fqdn : ''

@description('Resource id of the golden-signal alerts Action Group. Empty unless alerts are wired (enableAlerts AND enableAppInsights). Operators can attach extra receivers (webhook/SMS/ITSM) to it post-deploy.')
output alertsActionGroupId string = (enableAlerts && enableAppInsights) ? alerts!.outputs.actionGroupId : ''

@description('Resource id of the deployed operator Workbook. Empty unless Workbooks are wired (enableWorkbooks AND enableAppInsights). Open it first when an SLO alert fires.')
output operatorWorkbookId string = (enableWorkbooks && enableAppInsights) ? workbooks!.outputs.operatorWorkbookId : ''
