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

@description('''
Resource id of the user-assigned managed identity this app authenticates
as. Pre-created at the main.bicep top level so role assignments
(AcrPull on ACR, "Key Vault Secrets User" on KV) can be created BEFORE
the app exists — breaks the chicken-and-egg deadlock where a system-
assigned MI's principalId only materializes after revision creation,
but revision creation needs the role assignments to already exist
to pull the image / read KV secrets.
''')
param userAssignedIdentityId string

@description('''
Comma-separated list of browser origins allowed by CORS. Becomes
``MDK_CORS_ALLOWED_ORIGINS`` on the container. Empty string means
"no CORS configured" — the runtime defaults are still applied at
the application layer. See main.bicep param of the same name for the
operator-facing doc.
''')
param corsAllowedOrigins string = ''

@description('''
Name of the Container Apps Environment storage config that backs the
Azure Files agents volume. When non-empty, a volume named ``agents-vol``
is mounted at ``/home/movate/agents`` and ``MDK_AGENTS_PATH`` points
there instead of the image-baked ``/app/agents``. Empty string (default)
disables the mount — dev/staging with a single replica works fine on
pod-local storage.

Set this to ``'agents-vol'`` when ``useAzureFiles=true`` in main.bicep.
''')
param agentsStorageName string = ''

@description('Langfuse host URL (self-hosted). Empty string = the Langfuse SDK default (Cloud). Set by main.bicep to the self-hosted Langfuse app URL when deployLangfuse=true.')
param langfuseHost string = ''

@description('Common tags.')
param tags object = {}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    // User-assigned identity → pre-created at the main.bicep level so
    // role assignments are in place BEFORE this app's first revision
    // tries to pull from ACR / read KV. See userAssignedIdentityId
    // param doc above for the deadlock rationale.
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
        targetPort: 8000
        // HTTP only inside the env; ACA terminates TLS at the edge.
        transport: 'auto'
        // CORS for browser callers is handled inside the FastAPI app
        // (src/movate/runtime/app.py reads MDK_CORS_ALLOWED_ORIGINS, set
        // via the corsAllowedOrigins param threaded from main.bicep).
        // We don't use the ACA-platform-level `corsPolicy` here — the
        // app's middleware gives us per-route control and a single
        // source of truth across local + Azure.
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          // Identity-based pull. `identity` is the user-assigned MI's
          // resource id (pre-created at main.bicep top level). ACA uses
          // that MI to pull the image — AcrPull role assignment lives
          // on the MI, not on this app's runtime identity.
          identity: userAssignedIdentityId
        }
      ]
      // Key Vault references — secrets land in env vars without ever
      // being in the image or deployment outputs. Format:
      //   keyVaultUrl: <vault uri> + secrets/<secret name>
      //   identity: 'system' (managed identity reads KV)
      // The langfuse-* secrets are gated on `empty(langfuseHost)` (the
      // same flag that drives LANGFUSE_HOST below): when Langfuse is off
      // those Key Vault secrets don't exist, and referencing them here
      // would hard-fail revision provisioning ("unable to fetch secret
      // 'langfuse-secret-key'…"). The pg/provider/bootstrap secrets are
      // always required, so they stay unconditional.
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
        // The runtime's bootstrap key — populated by
        // `mdk auth bootstrap-seed <target> --keyvault <name>` BEFORE
        // the first runtime deploy. On every pod start,
        // _seed_bootstrap_key() inserts the matching ApiKeyRecord
        // into the api_keys table iff the row isn't already there.
        // This is what breaks the chicken-and-egg of "need a bearer
        // to mint a bearer" on fresh deployments and keeps the
        // operator's saved local key valid across revision recycles.
        {
          name: 'bootstrap-api-key'
          keyVaultUrl: '${keyVaultUri}secrets/bootstrap-api-key'
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
          name: 'movate-api'
          image: '${acrLoginServer}/${image}'
          command: ['movate']
          args: ['serve', '--host', '0.0.0.0', '--port', '8000']
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: concat([
            {
              name: 'MDK_DB_URL'
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
            // Bootstrap API key (see secrets[] above). The runtime's
            // _seed_bootstrap_key() reads this on startup and inserts
            // the matching ApiKeyRecord into Postgres if it isn't
            // already present. Operators run `mdk auth bootstrap-seed
            // <target>` once per environment to mint + upload this
            // secret value.
            {
              name: 'MDK_SEED_API_KEY'
              secretRef: 'bootstrap-api-key'
            }
            {
              name: 'MDK_AGENTS_PATH'
              // Pod-local when Azure Files is off (single-replica dev);
              // shared mount when agentsStorageName is set (multi-pod).
              value: empty(agentsStorageName) ? '/app/agents' : '/home/movate/agents'
            }
            {
              // Comma-separated browser-origin allow-list consumed by
              // the FastAPI CORSMiddleware in src/movate/runtime/app.py.
              // Empty string ("") is valid — keeps server-to-server
              // callers working while denying every browser preflight.
              // Set via main.bicep param so deploys are idempotent in
              // one Bicep apply (replacing the post-deploy
              // `az containerapp update --set-env-vars` step).
              name: 'MDK_CORS_ALLOWED_ORIGINS'
              value: corsAllowedOrigins
            }
          ], empty(langfuseHost) ? [] : [
            // Langfuse tracing creds + host — gated together with the
            // langfuse-* KV secret refs above. When Langfuse is off the
            // secrets don't exist, so neither the secretRef env vars nor
            // the host are emitted (the SDK keeps its Cloud default).
            {
              name: 'LANGFUSE_SECRET_KEY'
              secretRef: 'langfuse-secret-key'
            }
            {
              name: 'LANGFUSE_PUBLIC_KEY'
              secretRef: 'langfuse-public-key'
            }
            {
              // Point tracing at the self-hosted Langfuse (omitted when
              // empty so the SDK keeps its Cloud default).
              name: 'LANGFUSE_HOST'
              value: langfuseHost
            }
          ])
          volumeMounts: empty(agentsStorageName) ? [] : [
            {
              volumeName: 'agents-vol'
              mountPath: '/home/movate/agents'
            }
          ]
          probes: [
            {
              // Liveness stays on /healthz: unconditional 200,
              // independent of storage. A DB blip shouldn't trigger
              // a pod restart that wouldn't help (the new pod hits
              // the same DB).
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              // Readiness hits /ready: deep checks (storage ping).
              // Failure means "stop routing traffic to this pod"
              // without restarting it — the right move when a
              // dependency is down. Pod returns to the load
              // balancer once the dependency recovers.
              type: 'Readiness'
              httpGet: {
                path: '/ready'
                port: 8000
              }
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      volumes: empty(agentsStorageName) ? [] : [
        {
          name: 'agents-vol'
          storageType: 'AzureFile'
          // storageName references the managedEnvironment/storages binding
          // (Microsoft.App/managedEnvironments/storages) created in main.bicep.
          storageName: agentsStorageName
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
output fqdn string = api.properties.configuration.ingress.fqdn
output appResourceId string = api.id
@description('ACR id passthrough — main.bicep needs both this and the user-assigned MI principalId together for the role assignment.')
output acrResourceIdEcho string = acrResourceId
// Note: there is intentionally no principalId output here. With the
// UserAssigned identity model, ``api.identity.principalId`` is empty —
// the meaningful principalId lives on the UAI resource in main.bicep,
// which is also where role assignments live. Consumers that need the
// principalId should reference the UAI directly.
