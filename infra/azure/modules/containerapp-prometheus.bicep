// Prometheus Container App — high-resolution operational-metrics backend
// (ADR 087). Receives metrics PUSHED by the OTel collector's
// `prometheusremotewrite` exporter (D1) and serves them to Grafana over PromQL.
//
// Reachability: INTERNAL ingress only, HTTP on :9090. The collector writes to
//   http://<this-fqdn>/api/v1/write and Grafana queries http://<this-fqdn>.
//
// Dev posture (ADR 087 D2): single replica, EPHEMERAL TSDB (no volume) — history
//   is disposable in dev. NOT for production (→ Azure Monitor Managed Prometheus,
//   a follow-up ADR). The collector exporter is the seam for that migration.
//
// Config: Prometheus needs a --config.file, but with remote-write ingestion it
//   needs NO scrape_configs. We write a minimal config to the writable data dir
//   via the container command and enable the remote-write receiver — so no file
//   mount / custom image is required (stock prom/prometheus).

@description('Container App resource name, e.g. movate-dev-prometheus.')
param name string

@description('Azure region.')
param location string

@description('Managed Environment resource id the app deploys into.')
param environmentId string

@description('Prometheus image (pinned). Override to bump.')
param image string = 'docker.io/prom/prometheus:v2.54.1'

@description('TSDB retention (dev: short; history is disposable).')
param retention string = '6h'

@description('Resource tags.')
param tags object = {}

resource prometheus 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      // Internal-only: the collector (push) and Grafana (query) are both in this
      // environment; Prometheus is never publicly exposed.
      ingress: {
        external: false
        targetPort: 9090
        transport: 'http'
        allowInsecure: true
      }
    }
    template: {
      containers: [
        {
          name: 'prometheus'
          image: image
          // Write a minimal config (no scrape_configs — data arrives via
          // remote-write) to the writable data dir, then exec Prometheus with
          // the remote-write receiver enabled. ADR 087 D1/D2.
          command: [
            '/bin/sh'
            '-c'
          ]
          args: [
            'printf \'global:\\n  scrape_interval: 30s\\n\' > /prometheus/prometheus.yml && exec /bin/prometheus --config.file=/prometheus/prometheus.yml --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=${retention} --web.enable-remote-write-receiver --web.enable-lifecycle'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/-/healthy'
                port: 9090
              }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/-/ready'
                port: 9090
              }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        // Single replica: a TSDB is single-writer, and dev needs no HA. min=max=1
        // also keeps remote-write pointed at one stable target.
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

@description('Internal ingress FQDN of Prometheus. Collector writes to https://<this>/api/v1/write; Grafana queries https://<this>.')
output fqdn string = prometheus.properties.configuration.ingress.fqdn
