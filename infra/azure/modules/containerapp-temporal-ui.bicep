// Temporal Web UI on Azure Container Apps (ADR 078 D6) — browse workflows,
// histories, task queues, and pending signals instead of grepping container
// logs. Runs the public ``temporalio/ui`` image; connects to the self-hosted
// Temporal frontend's internal gRPC address (:7233). No persistence, no
// secrets → no managed identity, no registries block (public Docker Hub image).
//
// ⚠ AUTH: this UI is UNAUTHENTICATED and exposes all workflow data (inputs,
// results, histories — potentially sensitive). It's gated behind `enableTemporalUi`
// (default off) and, when external, should be IP-restricted or fronted by SSO
// for anything beyond a dev/POC. Default ingress is INTERNAL (browse via the CAE
// / a port-forward); flip `external=true` only when you accept public exposure.

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
the Container Apps Environment). Set true ONLY for dev/POC where unauthenticated
public access to workflow data is acceptable — prefer IP restrictions / SSO
otherwise.
''')
param external bool = false

@description('Public origin of this UI (https://<name>.<env-default-domain>) — used for CORS.')
param publicUrl string = ''

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
  properties: {
    environmentId: environmentId
    configuration: {
      ingress: {
        external: external
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      // temporalio/ui is a public Docker Hub image — no registries / identity.
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

@description('URL of the Temporal Web UI (empty when internal-only).')
output url string = external ? 'https://${ui.properties.configuration.ingress.fqdn}' : ''

@description('Temporal UI Container App resource id.')
output containerAppId string = ui.id
