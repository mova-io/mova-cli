# Deploying Temporal (server + worker) on Azure

How to stand up the **self-hosted Temporal server** (ADR 078) and the **Temporal
worker** (ADR 080 D1) in the movate Container Apps Environment so
`runtime: temporal` workflows — including durable HITL (ADR 062) — run
end-to-end. Native is unaffected; this is all gated on `enableTemporal`.

> **Alternative:** to use **Temporal Cloud** instead of self-hosting, leave
> `enableTemporal=false` and set `TEMPORAL_HOST=<ns>.tmprl.cloud:7233` +
> `TEMPORAL_TLS_CERT` on the api/worker (BYOK, ADR 054 D5). The rest of this
> runbook is for the self-hosted path.

## What gets deployed (when `enableTemporal=true`)
- `movate-<env>-temporal` — Temporal server (`temporalio/auto-setup`), internal
  gRPC `:7233`, backed by `temporal` + `temporal_visibility` DBs on the shared
  Postgres.
- `movate-<env>-temporal-worker` — runs `movate worker --backend temporal`,
  polls the task queue, executes workflows.
- The api + worker get `TEMPORAL_HOST=movate-<env>-temporal.internal.<domain>:7233`.

## Prerequisites
- The PRs are merged to `main` (or deploy from the `feat/temporal-durable-hitl`
  branch): #710 (server + worker + HITL + terminal sync) — that branch is
  self-contained for the durable path.
- `az login`; the resource group + the standard movate infra already provisioned
  (first-pass infra: KV, Postgres, ACR, CAE — the usual two-pass story).
- Key Vault holds the secrets the worker/server read: `pg-admin-password`,
  `openai-api-key`, `anthropic-api-key` (already required by the api/worker).

## Steps

**1. Build + push the image** (the image already includes the `[temporal]` extra
via `uv sync --all-extras`):
```bash
az acr build -r <acrName> -t movate:<tag> -f Dockerfile .
```

**2. Enable Temporal in the bicepparam** (`main.<env>.bicepparam`):
```bicep
param enableApiWorker = true     // required — the apps consume TEMPORAL_HOST
param enableTemporal  = true
param image           = 'movate:<tag>'
```

**3. Deploy:**
```bash
az deployment group create -g <rg> -f infra/azure/main.bicep -p main.<env>.bicepparam
```
This creates the `temporal` + `temporal_visibility` databases, the Temporal
server Container App (auto-setup runs the idempotent schema setup on first boot),
the Temporal worker Container App, and threads `TEMPORAL_HOST` into the api +
worker.

> First boot: the server runs schema setup against Postgres (~30–60s). The
> worker may restart a few times until the server's `:7233` frontend is
> reachable — ACA recovers it automatically.

**4. Mark a workflow durable** — in its `workflow.yaml`:
```yaml
runtime: temporal
```
Redeploy the image (or mount agents) so the worker picks it up; the
`movate-<env>-temporal-worker` registers every `runtime: temporal` workflow on
startup.

## Validate (the ADR 080 D3 acceptance gate)
1. **Server up:** `az containerapp logs show -n movate-<env>-temporal -g <rg>` —
   expect schema-setup success + "Started Worker"/frontend listening on 7233.
2. **Worker connected:** `az containerapp logs show -n movate-<env>-temporal-worker
   -g <rg>` — expect "registered N workflows" with no connection errors.
3. **End-to-end durable HITL:**
   - Start a `runtime: temporal` workflow with a HUMAN node.
   - `GET /api/v1/workflow-runs?status=paused` → the run appears (pause record
     written by `call_human_activity`).
   - `POST /api/v1/workflow-runs/{id}/signal` with the decision → the endpoint
     signals the Temporal handle (ADR 062 D2).
   - After resume completes: `GET /api/v1/workflow-runs/{id}` → status `success`,
     `runtime: temporal`, final state present, and it's **gone from the paused
     list** (terminal-state sync, ADR 080 D2).

## Resolved on first deploy (validated against movate-dev, ADR 078/080 D3)
- **ACA internal TCP ingress ↔ gRPC :7233 — WORKS.** The worker connected to
  `movate-dev-temporal.internal…:7233` over the `transport: tcp` internal ingress.
- **Azure Postgres extension allow-list.** The visibility schema needs
  `btree_gin` + `pg_trgm`; Azure blocks `CREATE EXTENSION` until allow-listed in
  `azure.extensions`. Fixed in `postgres.bicep` (`createTemporalDatabases` adds
  `BTREE_GIN,PG_TRGM` alongside `VECTOR`).
- **auto-setup TLS uses TWO env prefixes (the subtle one).** Azure PG mandates
  SSL (`require_secure_transport=on`). The schema **setup tool** reads
  `POSTGRES_TLS_*`; the **server's** runtime config reads `SQL_TLS_*`. Setting
  only `POSTGRES_TLS_*` makes schema setup succeed over SSL but the server
  connect *without* TLS → Postgres rejects it ("no usable database connection").
  Fixed in `containerapp-temporal.bicep`: also set `SQL_TLS_ENABLED=true`,
  `SQL_TLS_ENABLE_HOST_VERIFICATION=false`, `SQL_TLS_SERVER_NAME=<pg-fqdn>`.
  (Quick unblock if you hit this before the fix lands: temporarily
  `az postgres flexible-server parameter set … --name require_secure_transport
  --value off`, then re-enable once the `SQL_TLS_*` env is deployed.)

## Limits (v1)
- The server is a **single non-HA replica** (ADR 078 D6): a restart briefly
  pauses progress (durable state is safe in Postgres; in-flight workflows resume).
  HA (multi-service split) is a follow-up.
- The worker scales on a fixed replica count (Temporal distributes tasks); a
  task-queue-backlog autoscaler is a follow-up (ADR 080 Tier 2).
