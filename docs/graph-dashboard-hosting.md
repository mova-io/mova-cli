# Hosting the graph dashboard on Azure Container Apps

**Status:** Design note (not an ADR; no structural decision)
**Follows:** ADR 053 (hosted playground) — mirrors the same ACA hosting pattern

---

## Overview

The `mdk graph dashboard` is a **static HTML/CSS/JS viewer** that talks to the
runtime's graph query API through a local proxy (same as `mdk graph serve`).
Hosting it on Azure Container Apps follows the **same pattern as the hosted
playground** (ADR 053): one new ACA app in the runtime's environment, with
Entra Easy Auth gating access.

## Architecture

```
        invited user (browser)
              |  https://<graph-dashboard>.<region>.azurecontainerapps.io
              v
    +------------------------------------------+
    |  ACA built-in auth (Easy Auth) + Entra    |   <-- org SSO + B2B guests
    +------------------------------------------+
              |
              v
    +------------------------------------------+
    |  containerapp-graph-dashboard.bicep (NEW) |
    |  image: movate-cli + graph dashboard      |
    |  cmd: mdk graph dashboard                 |
    |       --host 0.0.0.0 --port 8901          |
    |       --target <env> --project <id>       |
    |       --no-open                           |
    |  env:                                     |
    |    MDK_TARGET_URL -> runtime internal FQDN|
    |    runtime bearer <- Key Vault secret-ref |
    +------------------------------------------+
              |
              v
    +------------------------------------------+
    |  containerapp-api.bicep (EXISTING)        |
    |  GET /api/v1/projects/{id}/graph          |
    |  GET /api/v1/graph/nodes/{id}             |
    |  GET /api/v1/graph/search                 |
    |  GET /api/v1/.../graph/analytics/*        |
    |  GET /api/v1/.../graph/stream (SSE)       |
    +------------------------------------------+
```

## Bicep module shape

A new `infra/azure/modules/containerapp-graph-dashboard.bicep` would follow
the `containerapp-playground.bicep` pattern (ADR 053 D1):

```bicep
// Parameters (same pattern as containerapp-playground.bicep)
param envId string           // Container App environment id
param registryServer string  // ACR login server
param imageName string       // e.g. 'movate-cli:latest'
param keyVaultUri string     // Key Vault URI for the scoped bearer
param targetUrl string       // Runtime internal FQDN
param project string         // Default project id
param minReplicas int = 0    // Scale-to-zero at idle

resource graphDashboard 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'graph-dashboard'
  properties: {
    managedEnvironmentId: envId
    configuration: {
      ingress: {
        external: true          // Shareable URL
        targetPort: 8901
        transport: 'http'
      }
      // Easy Auth configured on this app (same as playground)
    }
    template: {
      containers: [{
        name: 'graph-dashboard'
        image: '${registryServer}/${imageName}'
        command: [
          'mdk', 'graph', 'dashboard',
          '--host', '0.0.0.0',
          '--port', '8901',
          '--target', '<env>',
          '--project', project,
          '--no-open'
        ]
        env: [
          { name: 'MDK_TARGET_URL', value: targetUrl }
          // Bearer via Key Vault secret-ref (never in env literal)
        ]
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
      }]
      scale: {
        minReplicas: minReplicas    // 0 = scale-to-zero at idle
        maxReplicas: 3
      }
    }
  }
}
```

## Auth: Entra Easy Auth

Same as ADR 053 D4:

- **ACA built-in authentication (Easy Auth)** fronts the dashboard's ingress.
- **Entra ID** provides org SSO; external testers are added as B2B guests.
- **Zero app-auth code** -- the dashboard is static HTML/JS; auth is at the
  platform layer.

## The runtime provides all data

The dashboard is a **static-serving container** that proxies API calls to the
runtime. All graph data (nodes, edges, analytics, search, streaming) comes from
the runtime's existing graph query API (ADR 046). The dashboard adds no data
layer of its own.

Endpoints used:
- `GET /api/v1/projects/{id}/graph?mode=knowledge` -- full graph
- `GET /api/v1/graph/nodes/{id}` -- node detail + provenance
- `GET /api/v1/graph/nodes/{id}/neighbors` -- expand
- `GET /api/v1/projects/{id}/graph/search?q=...` -- entity search
- `GET /api/v1/projects/{id}/graph/analytics/centrality` -- centrality
- `GET /api/v1/projects/{id}/graph/analytics/path` -- shortest path
- `GET /api/v1/projects/{id}/graph/analytics/communities` -- communities
- `GET /api/v1/projects/{id}/graph/stream` -- live growth SSE
- `GET /api/v1/projects` -- project list (for the switcher)

## Cost estimate

- **At idle (scale-to-zero ACA): ~$0/month.** The container scales to zero
  replicas when no requests arrive; ACA charges nothing for zero replicas.
- **Under load:** minimal -- the dashboard is static-serving + proxying; the
  CPU/memory is tiny (0.25 vCPU, 0.5 GiB). ACA per-second billing means cost
  tracks actual usage. The runtime (which does the real work) is already
  provisioned and costed separately.

## Deploy path

When `mdk deploy` gains `--mode graph-dashboard` (additive, same pattern as
`--mode playground` from ADR 053 D5), it would:

1. Build the image with the graph dashboard assets included.
2. Provision the `containerapp-graph-dashboard.bicep` module.
3. Output the dashboard FQDN.

Until then, the dashboard can be deployed manually by building the image and
running `az containerapp create` with the parameters above.

## Local development

`mdk graph dashboard --target <env> --project <id>` runs the dashboard
locally on `127.0.0.1:8901` (same as today). The hosted version is the
same binary with `--host 0.0.0.0` and Easy Auth in front.
