// Self-hosted Temporal server on Azure Container Apps (ADR 078).
//
// Runs the official ``temporalio/auto-setup`` image as a SINGLE Container App
// inside the shared movate Container Apps Environment, backed by the shared
// Postgres Flexible Server (dedicated ``temporal`` + ``temporal_visibility``
// databases created by postgres.bicep when enableTemporal=true).
//
// Topology (ADR 078 D1/D6): auto-setup bundles the four Temporal services
// (frontend / history / matching / internal-worker) + schema setup into one
// process. This is the single-cluster, NON-HA topology — pinned at ONE
// replica. Durable state lives in Postgres and survives a restart; the
// frontend is briefly unavailable while the container recycles (in-flight
// workflows resume automatically). The production multi-service split is
// ADR 078 Phase 3.
//
// Reachability (ADR 078 D2/D5): INTERNAL ingress, raw gRPC over TCP on :7233.
// The frontend is NEVER publicly exposed — the CAE network boundary is the
// control. The movate worker/API reach it at the internal FQDN and set
// TEMPORAL_HOST=<fqdn>:7233 (main.bicep wires this). No application code
// change — _resolve_temporal_connection reads TEMPORAL_HOST (ADR 054 D5).
//
// Datastore (ADR 078 D3): Azure Postgres mandates SSL; auto-setup connects
// with TLS enabled + host-verification disabled (the server cert is Azure's,
// not Temporal's; the network boundary is the control, D5). The databases are
// pre-created by postgres.bicep, so SKIP_DB_CREATE=true — auto-setup only runs
// the (idempotent) schema setup, not CREATE DATABASE. Standard SQL visibility
// (no Elasticsearch).
//
// Image note: ``temporalio/auto-setup`` is a public Docker Hub image — no
// ``registries`` block needed; only the managed identity (for the KV Postgres
// password) is used. Same as langfuse.bicep.
//
// SPIKE (ADR 078 Phase 1, flagged per CLAUDE.md §11): validate that ACA
// internal TCP ingress + the Temporal frontend interoperate, and that the
// auto-setup Postgres-TLS env names below match the pinned image tag. Tune
// here if the spike finds drift.

@description('Container App name.')
param name string

@description('Azure region.')
param location string

@description('Container Apps Environment id.')
param environmentId string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('''
Resource id of the user-assigned managed identity this app authenticates as.
Pre-created at main.bicep top level + granted "Key Vault Secrets User" BEFORE
this app's first revision reads KV — same cold-deploy deadlock avoidance as the
api / worker / langfuse apps.
''')
param userAssignedIdentityId string

@description('Postgres Flexible Server FQDN (the shared movate server).')
param postgresFqdn string

@description('Postgres admin username (auto-setup connects + runs schema setup as this).')
param postgresAdminUsername string

@description('Default (history) database name — pre-created by postgres.bicep.')
param defaultDbName string = 'temporal'

@description('Visibility database name — pre-created by postgres.bicep.')
param visibilityDbName string = 'temporal_visibility'

@description('Key Vault secret name holding the Postgres admin password.')
param postgresPasswordSecretName string = 'pg-admin-password'

@description('Temporal auto-setup image. PIN a tag (never :latest) so schema setup is reproducible + idempotent.')
param image string = 'temporalio/auto-setup:1.25.2'

@description('Temporal namespace auto-created on first boot.')
param defaultNamespace string = 'default'

@description('CPU per replica. Temporal bundles four services in one process — give it a full core.')
param cpu string = '1.0'

@description('Memory per replica.')
param memory string = '2.0Gi'

@description('Common tags.')
param tags object = {}

resource temporal 'Microsoft.App/containerApps@2024-03-01' = {
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
      // INTERNAL gRPC over raw TCP — never publicly exposed (ADR 078 D2/D5).
      // Reachable only as the internal FQDN on :7233 from other apps in this
      // Container Apps Environment.
      ingress: {
        external: false
        transport: 'tcp'
        targetPort: 7233
        exposedPort: 7233
      }
      // temporalio/auto-setup is a public Docker Hub image — no `registries`.
      secrets: [
        {
          name: 'pg-password'
          keyVaultUrl: '${keyVaultUri}secrets/${postgresPasswordSecretName}'
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'temporal'
          image: image
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            // --- datastore: shared Azure Postgres, TLS required (ADR 078 D3)
            {
              name: 'DB'
              value: 'postgres12' // Postgres 12+ persistence plugin
            }
            {
              name: 'POSTGRES_SEEDS'
              value: postgresFqdn
            }
            {
              name: 'DB_PORT'
              value: '5432'
            }
            {
              name: 'POSTGRES_USER'
              value: postgresAdminUsername
            }
            {
              name: 'POSTGRES_PWD'
              secretRef: 'pg-password'
            }
            {
              name: 'DBNAME'
              value: defaultDbName
            }
            {
              name: 'VISIBILITY_DBNAME'
              value: visibilityDbName
            }
            {
              // Databases are pre-created by postgres.bicep — run only the
              // (idempotent) schema setup, not CREATE DATABASE.
              name: 'SKIP_DB_CREATE'
              value: 'true'
            }
            {
              name: 'SKIP_SCHEMA_SETUP'
              value: 'false'
            }
            // --- TLS to Azure Postgres (which mandates SSL: require_secure_transport=on).
            // auto-setup uses TWO env prefixes for TLS, and BOTH must be set:
            //   * POSTGRES_TLS_* — read by the SCHEMA SETUP tool (temporal-sql-tool).
            //   * SQL_TLS_*      — read by the SERVER's config_template.yaml for its
            //                      runtime persistence connection.
            // We previously set only POSTGRES_TLS_*, so schema setup worked over SSL
            // but the server connected WITHOUT TLS → Postgres rejected it ("no usable
            // database connection"). Both stores' runtime connections need SQL_TLS_*.
            // Host verification is OFF (TLS-encrypted but no CA verification — same
            // posture as the apps' sslmode=require); this lets require_secure_transport
            // stay ON without bundling Azure's CA cert.
            {
              name: 'POSTGRES_TLS_ENABLED'
              value: 'true'
            }
            {
              name: 'POSTGRES_TLS_DISABLE_HOST_VERIFICATION'
              value: 'true'
            }
            {
              name: 'SQL_TLS_ENABLED'
              value: 'true'
            }
            {
              name: 'SQL_TLS_ENABLE_HOST_VERIFICATION'
              value: 'false'
            }
            {
              // SNI / server name for the TLS handshake to Azure PG.
              name: 'SQL_TLS_SERVER_NAME'
              value: postgresFqdn
            }
            {
              // Standard SQL visibility on Postgres — no Elasticsearch (D3).
              name: 'ENABLE_ES'
              value: 'false'
            }
            {
              // Bind the frontend on the container IP so it's reachable via
              // the internal ingress.
              name: 'BIND_ON_IP'
              value: '0.0.0.0'
            }
            {
              name: 'DEFAULT_NAMESPACE'
              value: defaultNamespace
            }
          ]
          probes: [
            {
              // TCP socket on the frontend gRPC port. Generous initial delay —
              // the first boot runs schema setup against Postgres.
              type: 'Liveness'
              tcpSocket: {
                port: 7233
              }
              initialDelaySeconds: 60
              periodSeconds: 30
              failureThreshold: 5
            }
          ]
        }
      ]
      // ADR 078 D6: ONE logical Temporal cluster — pinned at a single replica.
      // This is NOT horizontally scalable; HA is the Phase-3 multi-service
      // split. Durable state is safe in Postgres across a restart.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

@description('Internal FQDN of the Temporal frontend (no scheme/port).')
output internalFqdn string = temporal.properties.configuration.ingress.fqdn

@description('Temporal frontend host:port — set as TEMPORAL_HOST on the movate apps.')
output temporalHost string = '${temporal.properties.configuration.ingress.fqdn}:7233'

@description('Temporal Container App resource id.')
output containerAppId string = temporal.id
