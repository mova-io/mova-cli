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

## Image parity — keep the worker images in lock-step with the api

The api, queue worker, and `temporal-worker` Container Apps must run the **same
image** (the bicep deploys all three from one `image` param). A runtime feature
only lands on the worker if its image matches — durable-HITL / ADR 080
terminal-state sync runs on the **worker** image, so an api-only roll leaves it
silently no-op'ing. The drift sneaks in via a manual `mdk deploy --only
api|worker` or a partial deploy that advanced one app but not the others.

`mdk deploy` guards against this automatically: after rolling the Container
Apps it reads each app's configured image back
(`az containerapp show … --query properties.template.containers[0].image`) and
**fails loudly (exit 1)** if `movate-<env>-api`, `-worker`, and (when Temporal
is enabled) `-temporal-worker` don't all match. It acts only on a confirmed
mismatch — a single-app target or an `az` hiccup degrades to a no-op rather than
blocking a rollout. Pass `--skip-image-parity-check` for an intentional partial
roll, then reconcile with a full deploy. To check by hand:

```bash
for app in api worker temporal-worker; do
  echo "movate-<env>-$app: $(az containerapp show -g <rg> -n movate-<env>-$app \
    --query properties.template.containers[0].image -o tsv)"
done
```

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

   **One-command smoke (codifies step 3):**
   ```bash
   RUNTIME_URL=https://movate-<env>-api.<domain> API_KEY=mvt_live_... \
     WORKFLOW=<deployed runtime:temporal workflow> \
     ./scripts/temporal-e2e-smoke.sh
   ```
   It submits the workflow, waits for the durable pause, signals the decision
   (override the default with `DECISION='{"decision":"approve", ...}'` to satisfy
   the gate's `output_contract`), and asserts the run resumes to **SUCCESS** —
   green proves server+worker liveness, the resume door, and that the **deployed
   worker image carries ADR 080 terminal-sync** (the image-drift check). Pair
   with `scripts/temporal-preflight.sh` (prereqs/health) for a full before/after.

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

## Temporal Web UI (optional) — and why external exposure needs auth

The deploy can also stand up the **Temporal Web UI** (ADR 078 D6,
`movate-<env>-temporal-ui`) — browse workflows, histories, task queues, and
pending HUMAN-node signals in a browser instead of grepping container logs. It's
gated on `enableTemporalUi` (default off) and requires `enableTemporal=true`.

> ⚠ **The `temporalio/ui` image has NO built-in authentication.** Anyone who can
> reach it sees **every workflow history** — inputs, results, and the
> **durable-HITL payloads** (ADR 062/083). Treat the UI's reachability as
> equivalent to read access to all workflow data.

**Default = INTERNAL, which is safe.** With `temporalUiExternal=false` (the
default) the UI has **internal** Container Apps ingress — reachable only from
inside the Container Apps Environment (browse via a port-forward / jumpbox), not
from the public internet. No auth is needed in this mode.

**External exposure REQUIRES auth.** Setting `temporalUiExternal=true` publishes
a public FQDN. To make that safe, the bicep fronts the UI with **Azure Container
Apps built-in authentication ("Easy Auth") + Entra ID** — the same pattern as the
graph/playground apps (ADR 053 D4). This is controlled by `temporalUiAuth`:

| `temporalUiExternal` | `temporalUiAuth` | Result |
| --- | --- | --- |
| `false` (default) | (ignored) | Internal ingress — not publicly reachable, no auth needed. |
| `true` | `true` (default) | Public FQDN, **gated behind Entra login** (Easy Auth). Requires `temporalUiEntraClientId`. |
| `true` | `false` | Public FQDN, **UNAUTHENTICATED** — anyone with the URL reads all workflow histories. Throwaway dev/POC only. |

Because `temporalUiAuth` defaults to **true**, flipping the UI external does not
silently expose it — you must explicitly opt out (`temporalUiAuth=false`) to get
the old unauthenticated behavior.

### Configure Easy Auth (the `temporalUiExternal=true` path)

Mirrors the [graph dashboard runbook](graph-app-deploy.md) Easy-Auth ceremony.
The Entra app registration and the Key Vault secret are **operator-run**; Bicep
provisions the **app**, never tenant identity.

```bash
ENV=dev
RG_NAME="movate-${ENV}-rg"
VAULT_NAME="movate-${ENV}-kv"             # append your nameSuffix if you set one
UI_NAME="movate-${ENV}-temporal-ui"
```

**1. Create the Entra app registration** (the redirect URI needs the UI's FQDN,
which you only learn after the first deploy — use a placeholder, fix it in step 4):
```bash
az ad app create --display-name "movate-temporal-ui-${ENV}" \
  --web-redirect-uris "https://PLACEHOLDER/.auth/login/aad/callback"
# note the printed appId → that's temporalUiEntraClientId
```

**2. Mint a client secret and store it in Key Vault** (ACA reads it via the
temporal-ui managed identity; the bicep grants that identity "Key Vault Secrets
User" on pass 1):
```bash
az ad app credential reset --id <appId> --append --query password -o tsv
az keyvault secret set --vault-name "$VAULT_NAME" \
  --name temporal-ui-entra-client-secret --value "<the-secret>"
```

**3. Set the parameters** in `main.<env>.bicepparam`:
```bicep
param enableTemporalUi     = true
param temporalUiExternal   = true
// param temporalUiAuth    = true            // default — Easy Auth ON
param temporalUiEntraClientId = '<appId from step 1>'
// param temporalUiEntraTenantId = ''        // empty → the deployment's own tenant
```

> If you set `temporalUiExternal=true` and leave `temporalUiAuth=true` (the
> default) but forget `temporalUiEntraClientId`, the deployment **fails at the
> `authConfig` resource** (Easy Auth needs a client id) rather than shipping a
> broken or open gate — fix the client id and re-deploy.

**4. Deploy, then finish the redirect URI:**
```bash
az deployment group create -g "$RG_NAME" \
  -f infra/azure/main.bicep -p infra/azure/main.${ENV}.bicepparam \
  --query "properties.outputs.temporalUiUrl.value" -o tsv
# → the SSO-gated UI URL. Now point the redirect URI at the real FQDN:
az ad app update --id <appId> \
  --web-redirect-uris "https://${UI_NAME}.<env-default-domain>/.auth/login/aad/callback"
```
Re-deploy (or roll a new revision) so Easy Auth accepts logins. Browse the URL →
Entra login → the Temporal UI loads. Anonymous requests are redirected to login
**before** they reach the container.

> **CI guard.** `infra/azure/main.bicep` (and every module) is compiled +
> linted on each PR by the `bicep` job in `.github/workflows/ci.yml`
> (`bicep build` + `bicep lint`), so a malformed authConfig or unused param is
> caught before anyone runs `az deployment group create`.

## Limits (v1)
- The server is a **single non-HA replica** (ADR 078 D6): a restart briefly
  pauses progress (durable state is safe in Postgres; in-flight workflows resume).
  HA (multi-service split) is a follow-up.
- The worker scales on a fixed replica count (Temporal distributes tasks); a
  task-queue-backlog autoscaler is a follow-up (ADR 080 Tier 2).
