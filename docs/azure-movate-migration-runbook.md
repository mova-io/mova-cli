# Azure migration runbook — personal sandbox → Movate sub

> ✅ **Status: COMPLETE 2026-05-14.** Migration landed end-to-end.
> New runtime live at
> `https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io`
> on the Movate `AZLABSV2.0-Sandbox(POC)` sub. Real OpenAI calls
> verified via `faq-agent` ($3.6e-05 cost, 35-output-token response,
> 2972ms latency — proves API → Worker → Postgres → KV → LLM all
> wired correctly).
>
> **Resolution of the earlier blocker:** Movate ops granted the SP
> `User Access Administrator` on the sub. The Bicep deploy is
> idempotent — re-running it picked up where it left off and added
> the 6 missing role assignments cleanly. ARM eventual-consistency
> on `listKeys()` for the freshly-created Log Analytics workspace
> tripped one extra retry; the second attempt succeeded.
>
> **What's still running:** the personal-sub stack (`61ea8b9b-...`)
> remains live as a fallback through the Friday demo. The
> `dev-personal` target in `~/.movate/config.yaml` points at it for
> easy access if anything goes sideways. Decommission per the
> "Post-cutover cleanup" section below once the demo confirms.

**Authored:** 2026-05-14
**Completed:** 2026-05-14 (same day)
**Strategy:** Blue/green — deploy fresh stack on Movate sub in parallel, verify, cut Deva over, decommission old infra after demo confirms working.

---

## Why we're doing this

The dev runtime that's serving Deva today lives on a personal pay-as-you-go subscription (`61ea8b9b-...`) anchored to a Gmail account. That was fine for prototyping, wrong for a customer-facing demo:

- Billing flows to the wrong place
- No Movate SSO / governance / audit
- One person's payment-method failure kills the demo
- Can't grant other Movate engineers Contributor for help

The Movate `AZLABSV2.0-Sandbox(POC)` sub (`8fab0f8f-...`) is now provisioned with the service principal `fe9e2bf7-...` granted Contributor. This runbook moves every running piece onto it.

## What's in scope

Everything currently in `movate-dev-rg` on the personal sub:

| Component | Source resource |
|---|---|
| API container app | `movate-dev-api` |
| Worker container app | `movate-dev-worker` |
| Container Apps Env | `movate-dev-cae` |
| Azure Container Registry | `movatedevacrjsy.azurecr.io` |
| Key Vault | `movate-dev-kv-jsy` |
| Postgres Flex Server | `movate-dev-pg-jsy` |
| Log Analytics | `movate-dev-logs` |
| Teams Bot (Container App + Bot Service) | `movate-dev-teams-bot` + `movate-dev-bot` |
| User-assigned managed identities | `movate-dev-api-mi`, `movate-dev-worker-mi`, `movate-dev-teams-bot-mi` |

## What stays UNTOUCHED until cutover

- The personal-sub stack keeps running. Deva's current bearer + URL keep working.
- We only flip the operator-side `~/.movate/config.yaml` + `scripts/deva-curl/.env` after the new stack passes smoke.
- Old stack is decommissioned ONLY after Friday's demo confirms the new URL works in the live wizard flow.

## What's not in scope

- DNS / vanity URL pointing at the new ACA fqdn — Friday demo uses whatever Azure generates. Custom domain comes post-demo.
- Data migration from the old Postgres to the new one. The current Postgres holds only Deva's exploratory runs from this week; they don't need to survive the migration. Fresh DB on the new sub.
- Old runtime decommission — covered in a separate post-cutover step at the bottom.

---

## Naming conventions on the new sub

To avoid conflicts with the personal sub's globally-unique names (KV, ACR, Postgres) we use a fresh suffix:

| Resource | Source name | Target name |
|---|---|---|
| Resource Group | `movate-dev-rg` (in personal sub) | `movate-dev-rg` (in Movate sub) |
| ACR | `movatedevacrjsy` | `movatedevacrmvt` |
| Key Vault | `movate-dev-kv-jsy` | `movate-dev-kv-mvt` |
| Postgres | `movate-dev-pg-jsy` | `movate-dev-pg-mvt` |
| Log Analytics | `movate-dev-logs` | `movate-dev-logs` |
| Container Apps Env | `movate-dev-cae` | `movate-dev-cae` |
| API app | `movate-dev-api` | `movate-dev-api` |
| Worker app | `movate-dev-worker` | `movate-dev-worker` |
| API URL | `movate-dev-api.victoriouswater-7958662f.eastus2.azurecontainerapps.io` | `movate-dev-api.<new-cae-hash>.eastus2.azurecontainerapps.io` |

RG / CAE / app names are RG-scoped, so they can repeat across subs. KV/ACR/Postgres live in global Azure namespaces; the `mvt` suffix distinguishes.

---

## Pre-flight checklist

| Check | How |
|---|---|
| Logged in as SP, pointed at Movate sub | `az account show` → `id = 8fab0f8f-...` and `user.name = fe9e2bf7-...` |
| SP has Contributor on the sub | `az group list -o table` returns rows (or empty list, no error) |
| Repo is on `main` at a clean HEAD | `git status` clean; `git rev-parse HEAD` shows latest |
| The PR for item 78 is merged (or its absence is OK) | `gh pr view 116 --json mergedAt` |
| All KV secret values are at hand | See [Secret inventory](#secret-inventory) below |

## Secret inventory

You'll need these values on hand to populate the new Key Vault. Most live in your password manager or `~/.zshrc` from the original setup:

| Secret name in KV | Source |
|---|---|
| `pg-admin-password` | Generate fresh: `openssl rand -base64 32 \| tr -d '/+=' \| cut -c1-32` |
| `pg-connection-string` | Auto-constructed by Bicep from FQDN + password — don't manually set |
| `openai-api-key` | Same `sk-proj-...` you used on the personal sub |
| `anthropic-api-key` | Same `sk-ant-...` you used on the personal sub |
| `langfuse-secret-key` | Same `sk-lf-...` |
| `langfuse-public-key` | Same `pk-lf-...` |
| `movate-teams-encryption-key` | Generate fresh Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `movate-teams-fleet-api-key` | (Re)mint after the API runtime is up: `movate auth create-key --tenant teams-bot-internal --env live --label teams-bot` |
| `microsoft-app-password` | Same value from the Bot Service AAD app (if you keep Teams enabled — otherwise skip) |

If you want to skip Teams entirely on the Movate sub for now, set `enableTeamsBot = false` in the new bicepparam and the last three secrets aren't needed.

---

## Migration steps

### Step 1 — Create the new bicepparam

Already in the repo at `infra/azure/main.movate.bicepparam` (gitignored, this runbook commits the `.example` template). Confirm it has:

- `env = 'dev'`
- `enableApiWorker = false` (first pass — KV doesn't exist yet to reference secrets)
- `nameSuffix = 'mvt'`
- `location = 'eastus2'`
- `postgresAdminPassword` (a strong literal — write it down; you'll re-paste into KV after first pass)
- `enableTeamsBot = false` (defer Teams to a v0.8 follow-up)

### Step 2 — Create the resource group

```bash
az group create \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --name movate-dev-rg \
    --location eastus2 \
    --tags application=movate environment=dev managedBy=bicep owner=jsyu
```

**Estimated time:** 10 seconds.

### Step 3 — First-pass Bicep deploy (infra only)

This provisions Log Analytics, ACR, Key Vault, Postgres, the Container Apps Environment, and the three user-assigned managed identities — but NOT the API / worker container apps (those would fail because KV is empty).

```bash
az deployment group create \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --resource-group movate-dev-rg \
    --template-file infra/azure/main.bicep \
    --parameters infra/azure/main.movate.bicepparam \
    --name "migrate-infra-$(date +%Y%m%d-%H%M%S)" \
    --query "{state:properties.provisioningState, kvUri:properties.outputs.keyVaultUri.value, acr:properties.outputs.acrLoginServer.value, pgFqdn:properties.outputs.postgresFqdn.value}" \
    -o json
```

**Estimated time:** 7-12 minutes. The Postgres Flex provisioning dominates.

**Verify:**
- `state = "Succeeded"`
- ACR FQDN: `movatedevacrmvt.azurecr.io`
- KV URI: `https://movate-dev-kv-mvt.vault.azure.net/`

### Step 4 — Populate the Key Vault (USER ACTION)

Bicep can't write secrets — they'd land in deployment outputs in plaintext, which is the exact thing KV exists to avoid. So this step is interactive.

Grant yourself + the SP the "Key Vault Secrets Officer" role on the new KV (only "Secrets User" is granted automatically, which is read-only):

```bash
az role assignment create \
    --assignee "$AZURE_CLIENT_ID" \
    --role "Key Vault Secrets Officer" \
    --scope "$(az keyvault show -n movate-dev-kv-mvt -g movate-dev-rg --query id -o tsv)"

# Yourself too, so you can paste secrets from the GUI or CLI
az role assignment create \
    --assignee "Jeremy.Yu@movate.com" \
    --role "Key Vault Secrets Officer" \
    --scope "$(az keyvault show -n movate-dev-kv-mvt -g movate-dev-rg --query id -o tsv)"
```

Then set each secret. Use the values from [Secret inventory](#secret-inventory):

```bash
# Replace <values> with the real ones from your password manager / ~/.zshrc

az keyvault secret set --vault-name movate-dev-kv-mvt \
    --name pg-admin-password \
    --value "<the password literal you put in main.movate.bicepparam>"

az keyvault secret set --vault-name movate-dev-kv-mvt \
    --name openai-api-key --value "<sk-proj-...>"

az keyvault secret set --vault-name movate-dev-kv-mvt \
    --name anthropic-api-key --value "<sk-ant-...>"

az keyvault secret set --vault-name movate-dev-kv-mvt \
    --name langfuse-secret-key --value "<sk-lf-...>"

az keyvault secret set --vault-name movate-dev-kv-mvt \
    --name langfuse-public-key --value "<pk-lf-...>"

# Postgres connection string — composed automatically by the bicep
# module, but if your modules don't auto-set it, fall back to:
az keyvault secret set --vault-name movate-dev-kv-mvt \
    --name pg-connection-string \
    --value "postgresql://movate@movate-dev-pg-mvt.postgres.database.azure.com:5432/movate?sslmode=require"
```

**Estimated time:** 5 minutes.

### Step 5 — Build + push the v0.7 image to the new ACR

```bash
GIT_SHA=$(git rev-parse --short HEAD)
IMAGE_TAG="movate:0.7.0-${GIT_SHA}"

az acr build \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --registry movatedevacrmvt \
    --image "${IMAGE_TAG}" \
    --image "movate:0.7.0-latest" \
    --file Dockerfile \
    --target runtime \
    .
```

**Estimated time:** 60-90 seconds cloud-side build.

### Step 6 — Second-pass Bicep deploy (API + worker enabled)

Edit `infra/azure/main.movate.bicepparam`:

```diff
- param enableApiWorker = false
+ param enableApiWorker = true
+ param image = 'movate:0.7.0-<sha>'
```

Then:

```bash
az deployment group create \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --resource-group movate-dev-rg \
    --template-file infra/azure/main.bicep \
    --parameters infra/azure/main.movate.bicepparam \
    --parameters image="${IMAGE_TAG}" \
                 corsAllowedOrigins="http://localhost:4200,${MOVA_IO_ORIGIN:-}" \
    --name "migrate-apps-$(date +%Y%m%d-%H%M%S)" \
    --query "{state:properties.provisioningState, apiUrl:properties.outputs.apiUrl.value}" \
    -o json
```

**Estimated time:** 3-5 minutes.

**Verify:** `state = "Succeeded"`; `apiUrl` is `https://movate-dev-api.<cae-hash>.eastus2.azurecontainerapps.io`. Save this URL — it's the new live URL.

### Step 7 — Smoke

```bash
API_URL=$(az containerapp show -g movate-dev-rg -n movate-dev-api \
    --query "properties.configuration.ingress.fqdn" -o tsv)
API_URL="https://${API_URL}"

# Wait for the first revision to flip ready
for i in $(seq 1 30); do
    status=$(curl -sS -o /dev/null -w "%{http_code}" "${API_URL}/healthz" --max-time 10 || echo 000)
    [[ "$status" == "200" ]] && echo "/healthz OK" && break
    echo "attempt ${i}: ${status}, retrying in 10s..."; sleep 10
done

# Confirm v1 routes including item 78's /publish are advertised
curl -s "${API_URL}/api/v1/openapi.json" | jq -r '.paths | keys[]' | grep "/api/v1/agents" | sort
```

Expected output includes:
- `/api/v1/agents`
- `/api/v1/agents/from-wizard`
- `/api/v1/agents/{name}`
- `/api/v1/agents/{name}/publish`
- `/api/v1/agents/{name}/runs`
- `/api/v1/agents/{name}/evals`
- `/api/v1/agents/{name}/validate`

### Step 8 — Mint Deva's new bearer

```bash
az containerapp exec \
    -g movate-dev-rg -n movate-dev-api \
    --command "mdk auth create-key --tenant-id deva-mova-io-demo \
        --env live --label deva-mova-io-friday-demo"
```

Copy the `mvt_live_...` string from the output.

**Note:** the demo on the OLD runtime uses a bearer minted against that runtime's API key table. The new bearer is a different secret — Deva needs both URL + bearer updated.

### Step 9 — Update operator state to point at the new runtime

```bash
# Update ~/.movate/config.yaml
yq -i '.targets.dev.url = "https://<new-fqdn>"' ~/.movate/config.yaml
yq -i '.targets.dev.azure_subscription = "8fab0f8f-b577-45d7-a485-ec32f73b22be"' ~/.movate/config.yaml
yq -i '.targets.dev.azure_acr_name = "movatedevacrmvt"' ~/.movate/config.yaml

# Update deva-curl wrapper bearer
sed -i '' 's/^MDK_TOKEN=.*$/MDK_TOKEN=<new-bearer>/' scripts/deva-curl/.env
sed -i '' 's|^MDK_BASE_URL=.*$|MDK_BASE_URL=https://<new-fqdn>|' scripts/deva-curl/.env

# Update MDK_DEV_KEY env var for your local shell
# (run this directly in your shell — don't commit)
export MDK_DEV_KEY="<new-bearer>"
```

### Step 10 — Send Deva the updated onboarding bundle

Template (Teams DM):

```
Heads up — moved the MDK runtime to Movate-owned Azure infra ahead of
tomorrow. Same shape, new URL + new bearer.

Runtime URL:    https://<new-fqdn>
OpenAPI:        https://<new-fqdn>/api/v1/openapi.json
Bearer:         <new mvt_live_...>
Old URL:        still up as a fallback through Friday afternoon

Replace the values in your front-end env config + retest one curl to
confirm; everything else (endpoints, payload shapes, CORS) is identical.

Any issue, ping me — old runtime stays live as a safety net.
```

### Step 11 — Verify Deva's curl-wrapper smoke against the new runtime

```bash
bash scripts/deva-curl/01-health.sh
bash scripts/deva-curl/02-list-agents.sh
bash scripts/deva-curl/05-create-via-wizard.sh
bash scripts/deva-curl/06-run-agent.sh smoke-bot '{"input":"hi"}' --wait
```

Every one should return 2xx. If any fails, **stop the cutover** — leave Deva pointed at the old URL, debug at leisure.

### Step 12 — Demo runs from the new URL

Friday morning, demo against `https://<new-fqdn>`. Old runtime stays alive on the personal sub as fallback through Friday afternoon.

---

## Post-cutover cleanup (Friday afternoon or Monday)

Once Friday's demo confirms the new runtime works under load:

### Decommission the old infra

```bash
# Switch context back to the personal sub
az account set --subscription 61ea8b9b-8be4-4f6f-9655-e1846c6082fb

# Soft-delete (kept 7d for recovery)
az group delete \
    -g movate-dev-rg \
    --yes \
    --no-wait
```

This stops the meter on the personal sub. Recovery window: Azure keeps the RG in soft-deleted state for ~7 days; running `az group create` with the same name within that window restores it. After 7d it's gone for good.

### Final operator-side cleanup

```bash
# Remove the old SP credential file if it exists
# (probably not applicable — SP was only ever set up on the Movate sub)

# Confirm ~/.movate/config.yaml has no stray references to 61ea8b9b
grep 61ea8b9b ~/.movate/config.yaml  # → no matches

# Remove the personal sub from `az` cache
az account clear  # nukes everything; re-login as SP afterwards
set -a; source ~/.movate/azure.env; set +a
az login --service-principal -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID"
```

---

## Rollback

If anything in steps 1-8 fails irrecoverably:

1. Leave Deva pointed at the OLD URL (it's still up).
2. Don't run step 9 (operator state update) — keep `~/.movate/config.yaml` pointing at the old runtime.
3. The half-built Movate-sub deployment is harmless. Either delete `movate-dev-rg` on the Movate sub, or leave it for the next attempt.

If something goes wrong **after** step 9 (operator state updated to new URL):

```bash
# Revert ~/.movate/config.yaml + deva-curl/.env to old values
git diff ~/.movate/config.yaml  # see what changed
# Manually restore the OLD URL + OLD bearer
```

Deva's old bearer still works on the old runtime — just point him back at the old URL.

---

## Decision log

| Decision | Rationale |
|---|---|
| Blue/green vs in-place | The personal sub IS still running; tearing it down before the new one is verified means no fallback. Blue/green gives us a free undo. |
| Fresh Postgres (no data migration) | The current DB only holds this week's exploratory runs. Migrating ~20 rows of test data isn't worth the complexity. |
| Defer Teams bot | Friday demo is Angular-only; Teams isn't on the demo path. Re-enable post-demo with `enableTeamsBot=true` on the new bicepparam. |
| `nameSuffix = 'mvt'` | `jsy` is already claimed in Azure global namespaces by the personal sub. `mvt` (Movate) is the natural successor. |
| Region: eastus2 (same as before) | No reason to change. ACA, Postgres Flex, and Bot Service all have GA support there. |
| Custom domain deferred | Friday demo is fine on `*.azurecontainerapps.io`; a vanity URL adds DNS + cert complexity for zero demo value. |
