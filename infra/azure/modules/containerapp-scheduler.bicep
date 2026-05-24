// movate-scheduler Container Apps **Job** — the portable cron substrate
// for the native scheduler (ADR 017 D2).
//
// Runs `mdk scheduler-tick` on a cron schedule. Each execution is a
// stateless one-shot: it drains BOTH the eval schedules (ADR 016) and the
// generic agent/workflow schedules (ADR 017), enqueuing a job for every
// due schedule into the SAME Postgres job queue the KEDA worker already
// drains. There is no in-process timer daemon — ACA Jobs is just the
// vendor-neutral cron that calls the tick (ADR 001: nothing movate-side
// imports a cloud SDK; off-Azure any cron runs the same command).
//
// Same image + managed identity + KV secret pattern as
// containerapp-worker.bicep. Gated off by default at the main.bicep level
// via `enableScheduler` so it's purely additive.

@description('Container Apps Job name.')
param name string

@description('Azure region.')
param location string

@description('Container Apps Environment id.')
param environmentId string

@description('ACR login server.')
param acrLoginServer string

@description('ACR resource id.')
param acrResourceId string

@description('Image tag, e.g. movate:2026.5.23.1.')
param image string

@description('Key Vault URI for secret references.')
param keyVaultUri string

@description('Postgres FQDN.')
param postgresFqdn string

@description('Postgres database name.')
param postgresDatabase string

@description('Postgres admin username.')
param postgresAdminUsername string

@description('CPU per execution.')
param cpu string = '0.25'

@description('Memory per execution.')
param memory string = '0.5Gi'

@description('''
Cron expression (5-field, UTC) for how often to run the tick. The tick is
idempotent and only enqueues schedules whose cadence has actually elapsed,
so running it more often than the finest schedule cadence is safe. Default
``*/5 * * * *`` = every 5 minutes.
''')
param cronExpression string = '*/5 * * * *'

@description('''
Resource id of the user-assigned managed identity this job authenticates
as. Reuse the worker UAI so the AcrPull + KV Secrets User role assignments
already in place cover the scheduler too (it pulls the same image + reads
the same secrets). Pre-created at main.bicep top level.
''')
param userAssignedIdentityId string

@description('Common tags.')
param tags object = {}

resource scheduler 'Microsoft.App/jobs@2024-03-01' = {
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
      // Scheduled (cron) trigger — the native ACA Jobs cron substrate.
      triggerType: 'Schedule'
      scheduleTriggerConfig: {
        cronExpression: cronExpression
        // One tick per fire; no parallelism (the tick is idempotent but a
        // single execution drains everything due).
        parallelism: 1
        replicaCompletionCount: 1
      }
      // A tick is short; cap it so a wedged execution can't run forever.
      replicaTimeout: 300
      replicaRetryLimit: 1
      registries: [
        {
          server: acrLoginServer
          // ACR pull via the (shared worker) user-assigned MI — AcrPull
          // role lives on the UAI, granted in main.bicep before this job.
          identity: userAssignedIdentityId
        }
      ]
      secrets: [
        {
          name: 'pg-password'
          keyVaultUrl: '${keyVaultUri}secrets/pg-admin-password'
          identity: userAssignedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'movate-scheduler'
          image: '${acrLoginServer}/${image}'
          command: ['mdk']
          // The unified tick: drains eval + generic agent/workflow schedules.
          args: ['scheduler-tick']
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              name: 'MDK_DB_URL'
              value: 'postgresql://${postgresAdminUsername}:@${postgresFqdn}:5432/${postgresDatabase}?sslmode=require'
            }
            {
              name: 'PGPASSWORD'
              secretRef: 'pg-password'
            }
          ]
        }
      ]
    }
  }
}

output schedulerName string = scheduler.name
output jobResourceId string = scheduler.id
output acrResourceIdEcho string = acrResourceId
