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
//
// WEEKLY SCHEDULED VARIANT — the template is parameterized on `triggerType`
// (Manual default, preserving the live movate-cert-suite job exactly;
// Schedule adds scheduleTriggerConfig with the weekly cron). A job's trigger
// type CANNOT be flipped in-place via the CLI: `az containerapp job update`
// exposes --cron-expression ("Only supported for trigger type 'Schedule'")
// but NO --trigger-type (that flag exists only on `job create`). So the
// weekly run deploys as a SECOND job from this same template:
//   az deployment group create -g movate-dev-rg \
//     --template-file infra/azure/containerapp-cert-job.bicep \
//     --parameters name=movate-cert-weekly triggerType=Schedule ...
// keeping movate-cert-suite Manual for on-demand certification gates.

@description('Container Apps Job name.')
param name string = 'movate-cert-suite'

@description('''
Job trigger type. Manual (default) = the on-demand certification gate
(the live movate-cert-suite shape). Schedule = the weekly cadence job
(deploy with name=movate-cert-weekly — ACA cannot change a job's trigger
type in-place, so the scheduled run is a separate job resource).
''')
@allowed([
  'Manual'
  'Schedule'
])
param triggerType string = 'Manual'

@description('''
Cron expression for triggerType=Schedule (UTC). Default: Mondays 14:00 UTC
= 07:00 PDT — a weekly certification heartbeat that keeps the Grafana
scenario x capability matrix fresh without burning LLM spend daily.
''')
param cronExpression string = '0 14 * * 1'

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
      // Manual (default) = on-demand gate; Schedule = the weekly cadence.
      // Exactly one of the trigger configs is set (null = omitted in ARM);
      // either way one suite run is the unit — scenarios share runtime
      // state, so a single sequential execution (parallelism 1 /
      // replicaCompletionCount 1) is required.
      triggerType: triggerType
      manualTriggerConfig: triggerType == 'Manual'
        ? {
            parallelism: 1
            replicaCompletionCount: 1
          }
        : null
      scheduleTriggerConfig: triggerType == 'Schedule'
        ? {
            cronExpression: cronExpression
            parallelism: 1
            replicaCompletionCount: 1
          }
        : null
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
