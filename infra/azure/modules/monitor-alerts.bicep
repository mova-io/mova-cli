// Azure Monitor golden-signal alert rules (item 27) — pages operators when the
// runtime's golden signals regress, beyond the application-level drift alerts
// (item 10, which fire via the NotificationDispatcher/webhook, not Azure
// Monitor).
//
// SIGNAL SOURCE — workspace-based App Insights:
//   The App Insights component (modules/appinsights.bicep) is workspace-based
//   (IngestionMode=LogAnalytics), so its telemetry lands in the EXISTING Log
//   Analytics workspace under the `App*` tables — NOT the classic
//   `dependencies`/`requests`/`customMetrics` tables. The OTel Collector
//   (ADR 020) forwards the runtime's OTLP to App Insights via its `azuremonitor`
//   exporter, which maps:
//     - spans      → AppDependencies   (Name == span name, e.g. "agent.execute")
//     - metrics    → AppMetrics        (Name == the OTel instrument name, e.g.
//                                        "mdk.jobs.completed" — dot-names are
//                                        preserved verbatim by the exporter)
//     - requests   → AppRequests       (HTTP server spans, e.g. /healthz, /api/v1)
//   so all four rules are LOG-SEARCH alerts (scheduledQueryRules v2) scoped to
//   the WORKSPACE, where the App* tables live.
//
// DEFAULT-OFF: invoked from main.bicep only when (enableAlerts && enableAppInsights).
// When enableAlerts=false, main.bicep does not instantiate this module at all,
// so ZERO Action Group / scheduledQueryRules resources are emitted.

@description('Resource id of the EXISTING Log Analytics workspace the workspace-based App Insights writes its App* tables to. The scheduledQueryRules are scoped here. Passed from logs.outputs.workspaceId in main.bicep.')
param workspaceResourceId string

@description('Resource id of the App Insights component (for cross-referencing / portal context only; the queries run against the workspace tables). Passed from appInsights.outputs.id. Stamped onto each rule as a tag so on-call can pivot from a fired alert straight to the component.')
param appInsightsId string

@description('App Insights component name — surfaced in alert descriptions so on-call knows which deployment fired. Passed from appInsights.outputs.name.')
param appInsightsName string

@description('Azure region for the Action Group + alert rules. scheduledQueryRules + actionGroups are RG-scoped, regional resources (actionGroups use a "Global" location convention but accept a region too; we keep them in-region for simplicity).')
param location string

@description('Action Group email receiver. Empty string (default) => the Action Group is still created (so alerts evaluate + surface in the portal Alerts blade) but with NO receiver, so nobody is paged until an operator wires one. Non-empty => an Email receiver named "primary" is added.')
param alertEmail string = ''

@description('Common tags applied to every resource.')
param tags object = {}

// --- Tunable evaluation cadence + windows ----------------------------------
// One window/eval pair shared across the rules keeps the alerting cadence
// uniform + easy to reason about. Operators can override per-deploy.

@description('Look-back window for each rule, ISO-8601 duration. Default 15m — long enough to smooth bursty per-job telemetry, short enough to page promptly.')
param windowSize string = 'PT15M'

@description('How often each rule is evaluated, ISO-8601 duration. Default 5m — three evaluations cover one 15m window.')
param evaluationFrequency string = 'PT5M'

// --- Per-signal thresholds (each a param with a default so operators tune) --

@description('Dead-letter spike: alert when the count of jobs reaching status=dead_letter over the window exceeds this. Default 1 — any dead-letter is worth a look in a healthy system.')
param deadLetterThreshold int = 1

@description('High error rate: alert when the failed-fraction of agent.execute spans over the window exceeds this (0..1). Default 0.10 = 10%.')
param errorRateThreshold int = 10

@description('High latency: alert when the p95 of agent.execute DurationMs over the window exceeds this (milliseconds). Default 30000 = 30s.')
param latencyP95ThresholdMs int = 30000

@description('Availability / no-traffic: this rule fires when the count of successful AppRequests over the window is AT or BELOW this. Default 0 — i.e. fire only on a complete traffic stall. Raise to require a minimum throughput.')
param minSuccessfulRequests int = 0

// --- Severities (Azure Monitor: 0=Critical .. 4=Verbose) --------------------

@description('Severity for the dead-letter spike alert (0=Critical..4=Verbose).')
@minValue(0)
@maxValue(4)
param deadLetterSeverity int = 1

@description('Severity for the high-error-rate alert.')
@minValue(0)
@maxValue(4)
param errorRateSeverity int = 1

@description('Severity for the high-latency alert.')
@minValue(0)
@maxValue(4)
param latencySeverity int = 2

@description('Severity for the availability / no-traffic alert.')
@minValue(0)
@maxValue(4)
param availabilitySeverity int = 1

// Stamp the monitored component's resource id onto each rule's tags so an
// on-call engineer can pivot from a fired alert to the App Insights component
// (and the rules carry a stable back-reference even though the KQL targets the
// workspace App* tables, not the component directly).
var ruleTags = union(tags, {
  'movate:appInsightsId': appInsightsId
})

// ---------------------------------------------------------------------------
// Action Group — the notification target wired to every rule below.
//
// Always created when this module is instantiated (i.e. when enableAlerts is
// true in main.bicep). The email receiver is added conditionally: with
// alertEmail empty, the receivers array is empty, so the group exists (alerts
// evaluate + show in the portal) but pages nobody until an operator adds a
// receiver. actionGroups must carry location 'Global'.
// ---------------------------------------------------------------------------

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${appInsightsName}-ag'
  location: 'Global'
  tags: tags
  properties: {
    // <=12 chars; surfaced as the SMS/voice sender + portal short name.
    groupShortName: 'movateslo'
    enabled: true
    emailReceivers: empty(alertEmail) ? [] : [
      {
        name: 'primary'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Golden-signal log-search alerts (scheduledQueryRules v2).
//
// api-version 2023-03-15-preview is the stable v2 shape `az bicep build`
// accepts; it carries the `criteria.allOf[].{query,timeAggregation,
// metricMeasureColumn,operator,threshold,failingPeriods}` schema + the
// `scopes`/`actions` wiring used below. All four are scoped to the WORKSPACE
// (App* tables live there for workspace-based App Insights).
// ---------------------------------------------------------------------------

// (1) DEAD-LETTER SPIKE — AppMetrics, instrument mdk.jobs.completed filtered to
// status=dead_letter. The azuremonitor exporter writes one AppMetrics row per
// reported metric with Name == the OTel instrument name and the attributes in
// the Properties bag (Properties.status). Sum the value over the window.
// ASSUMPTION (verify against the live workspace, 🔒): the exporter surfaces the
// `status` attribute under Properties["status"] and the counter value in the
// `Sum` column of AppMetrics. If the exporter instead lands these in
// customMetrics, swap the table name; the filter/agg are the same.
resource deadLetterRule 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${appInsightsName}-deadletter-spike'
  location: location
  tags: ruleTags
  properties: {
    displayName: 'movate: dead-letter spike'
    description: 'Jobs reaching status=dead_letter over ${windowSize} exceeded ${deadLetterThreshold} (mdk.jobs.completed, AppMetrics). Source: ${appInsightsName}.'
    severity: deadLetterSeverity
    enabled: true
    scopes: [workspaceResourceId]
    windowSize: windowSize
    evaluationFrequency: evaluationFrequency
    criteria: {
      allOf: [
        {
          query: '''AppMetrics
| where Name == "mdk.jobs.completed"
| where tostring(Properties["status"]) == "dead_letter"
| summarize DeadLetters = sum(Sum)'''
          timeAggregation: 'Total'
          metricMeasureColumn: 'DeadLetters'
          operator: 'GreaterThan'
          threshold: deadLetterThreshold
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// (2) HIGH ERROR RATE — AppDependencies, the agent.execute span emitted by the
// runtime (role movate-runtime). Failed fraction = failures / total over the
// window, expressed as a percentage to compare against errorRateThreshold.
// ASSUMPTION (🔒): the runtime span lands with Name=="agent.execute",
// AppRoleName=="movate-runtime", and Success is the bool the exporter sets from
// the span status. Confirm role name casing against live AppDependencies.
resource errorRateRule 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${appInsightsName}-high-error-rate'
  location: location
  tags: ruleTags
  properties: {
    displayName: 'movate: high error rate (agent.execute)'
    description: 'agent.execute failure rate over ${windowSize} exceeded ${errorRateThreshold}% (AppDependencies, role movate-runtime). Source: ${appInsightsName}.'
    severity: errorRateSeverity
    enabled: true
    scopes: [workspaceResourceId]
    windowSize: windowSize
    evaluationFrequency: evaluationFrequency
    criteria: {
      allOf: [
        {
          query: '''AppDependencies
| where Name == "agent.execute" and AppRoleName == "movate-runtime"
| summarize Total = count(), Failed = countif(Success == false)
| extend ErrorPct = iff(Total == 0, 0.0, todouble(Failed) * 100.0 / todouble(Total))
| project ErrorPct'''
          timeAggregation: 'Total'
          metricMeasureColumn: 'ErrorPct'
          operator: 'GreaterThan'
          threshold: errorRateThreshold
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// (3) HIGH LATENCY — AppDependencies p95 DurationMs of agent.execute over the
// window. percentile() over DurationMs (the exporter maps span duration to
// DurationMs, milliseconds).
resource latencyRule 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${appInsightsName}-high-latency-p95'
  location: location
  tags: ruleTags
  properties: {
    displayName: 'movate: high latency p95 (agent.execute)'
    description: 'agent.execute p95 DurationMs over ${windowSize} exceeded ${latencyP95ThresholdMs}ms (AppDependencies, role movate-runtime). Source: ${appInsightsName}.'
    severity: latencySeverity
    enabled: true
    scopes: [workspaceResourceId]
    windowSize: windowSize
    evaluationFrequency: evaluationFrequency
    criteria: {
      allOf: [
        {
          query: '''AppDependencies
| where Name == "agent.execute" and AppRoleName == "movate-runtime"
| summarize P95 = percentile(DurationMs, 95)
| project P95'''
          timeAggregation: 'Total'
          metricMeasureColumn: 'P95'
          operator: 'GreaterThan'
          threshold: latencyP95ThresholdMs
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// (4) API AVAILABILITY / NO-TRAFFIC — AppRequests successful count over the
// window. Fires when successful requests fall to/below minSuccessfulRequests
// (default 0 = total stall). AppRequests is the most robust availability signal
// in this schema: it's populated by HTTP server spans (the API's /healthz +
// /api/v1 traffic) the exporter maps to requests. Uses LessThanOrEqual so a
// flat-zero window trips it.
// ASSUMPTION (🔒): the API emits server spans the exporter lands in AppRequests
// with AppRoleName=="movate-runtime" and Success set from HTTP status. If the
// API role differs from the runtime role, relax/adjust the role filter.
resource availabilityRule 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: '${appInsightsName}-availability-no-traffic'
  location: location
  tags: ruleTags
  properties: {
    displayName: 'movate: API availability / no successful traffic'
    description: 'Successful AppRequests over ${windowSize} were <= ${minSuccessfulRequests} (API stall / outage). Source: ${appInsightsName}.'
    severity: availabilitySeverity
    enabled: true
    scopes: [workspaceResourceId]
    windowSize: windowSize
    evaluationFrequency: evaluationFrequency
    // checkWorkspaceAlertsStorageConfigured=false: store results in the rule,
    // not the workspace. autoMitigate stays on so the alert clears when traffic
    // resumes.
    criteria: {
      allOf: [
        {
          query: '''AppRequests
| where AppRoleName == "movate-runtime"
| summarize SuccessfulRequests = countif(Success == true)'''
          timeAggregation: 'Total'
          metricMeasureColumn: 'SuccessfulRequests'
          operator: 'LessThanOrEqual'
          threshold: minSuccessfulRequests
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

@description('Resource id of the Action Group all rules notify. Surfaced so main.bicep can echo it as a deployment output.')
output actionGroupId string = actionGroup.id

@description('Action Group name.')
output actionGroupName string = actionGroup.name
