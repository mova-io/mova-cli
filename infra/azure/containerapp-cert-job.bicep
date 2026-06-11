// movate-cert-suite Container Apps **Job** — the in-environment
// certification run.
//
// Runs `python -m certification.run_suite --target dev` against the deployed
// dev runtime FROM INSIDE the Container Apps environment, so (a) the
// side-effects column of the scenario x capability matrix lights up (the
// suite can reach Postgres/Temporal directly) and (b) the
// `mdk.certification.scenario` metrics actually ship — the OTLP collector
// (`movate-dev-otelcol`) is internal-ingress-only, unreachable from a laptop
// (see certification/run_suite.py). Trigger is Manual: an operator (or CI)
// starts an execution on demand with
//   az containerapp job start -g movate-dev-rg -n movate-cert-suite
//
// IaC capture of the live resource in movate-dev-rg (created imperatively
// 2026-06-11) — declaration only; this file is NOT yet wired into
// main.bicep (the CI bicep job compiles the main.bicep tree). Deploy
// standalone with `az deployment group create -g movate-dev-rg
// --template-file containerapp-cert-job.bicep ...` when reconciling.
//
// Same image + user-assigned-MI registry pattern as
// modules/containerapp-scheduler.bicep, but the secrets are passed as
// @secure() params rather than Key Vault references — matching how the live
// job was created (DSN + dev key set directly as job secrets).

@description('Container Apps Job name.')
param name string = 'movate-cert-suite'

@description('Azure region.')
param location string = resourceGroup().location

@description('Container Apps Environment id (movate-dev-cae).')
param environmentId string

@description('ACR login server.')
param acrLoginServer string = 'movatedevacrmvt.azurecr.io'

@description('Image tag, e.g. movate:2026.6.11.1.')
param image string

@description('''
Resource id of the user-assigned managed identity this job authenticates
as (movate-dev-worker-mi). Reuse the worker UAI so the AcrPull role
assignment already in place covers the cert job too (it pulls the same
movate image).
''')
param userAssignedIdentityId string

@description('Postgres DSN the suite verifies side-effects against (job secret pg-dsn).')
@secure()
param pgDsn string

@description('MDK dev API key the suite calls the runtime with (job secret dev-key).')
@secure()
param devKey string

@description('''
OTLP endpoint the suite ships `mdk.certification.scenario` metrics to —
the internal-ingress otel collector, only reachable from inside the
environment (which is the point of running the suite as an in-env job).
''')
param otlpEndpoint string = 'https://movate-dev-otelcol.internal.bluebush-9aec1e70.eastus2.azurecontainerapps.io'

@description('CPU per execution.')
param cpu string = '1.0'

@description('Memory per execution.')
param memory string = '2Gi'

@description('Common tags.')
param tags object = {}

resource certJob 'Microsoft.App/jobs@2024-03-01' = {
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
      // Manual trigger — the suite runs on demand (operator / CI), not on
      // a schedule: certification is an explicit gate, not a cron chore.
      triggerType: 'Manual'
      manualTriggerConfig: {
        // One suite run per start; scenarios share runtime state, so a
        // single sequential execution is the unit.
        parallelism: 1
        replicaCompletionCount: 1
      }
      // A full suite run (all scenarios, real LLM calls) takes minutes;
      // 1h caps a wedged execution without killing a slow-but-honest one.
      replicaTimeout: 3600
      // No retries: a failed certification run must surface as a failed
      // execution, never silently re-run (results would be ambiguous).
      replicaRetryLimit: 0
      registries: [
        {
          server: acrLoginServer
          // ACR pull via the shared worker user-assigned MI — AcrPull role
          // lives on the UAI.
          identity: userAssignedIdentityId
        }
      ]
      secrets: [
        {
          name: 'pg-dsn'
          value: pgDsn
        }
        {
          name: 'dev-key'
          value: devKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'movate-cert-suite'
          image: '${acrLoginServer}/${image}'
          // The suite entrypoint, run with the image's venv python so the
          // certification package (shipped at /app) resolves.
          command: [
            '/opt/movate/.venv/bin/python'
            '-m'
            'certification.run_suite'
            '--target'
            'dev'
          ]
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            {
              name: 'PYTHONPATH'
              value: '/app'
            }
            {
              name: 'MOVATE_DB_URL'
              secretRef: 'pg-dsn'
            }
            {
              name: 'MDK_DEV_KEY'
              secretRef: 'dev-key'
            }
            {
              name: 'OTEL_EXPORTER_OTLP_ENDPOINT'
              value: otlpEndpoint
            }
            {
              name: 'MDK_TRACE_SINK'
              value: 'both'
            }
          ]
        }
      ]
    }
  }
}

output certJobName string = certJob.name
output jobResourceId string = certJob.id
