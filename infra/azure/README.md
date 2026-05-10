# Azure deployment — operator walkthrough

End-to-end recipe for getting movate running on Azure. Covers a fresh
deployment from zero; for incremental updates (new image, scale
changes), skip to the [Update flow](#update-flow) section.

Architecture, naming, SKU choices: see [docs/v1.0-azure-design.md](../../docs/v1.0-azure-design.md).

## Prerequisites

- Azure subscription with `Contributor` + `User Access Administrator`
  on the target subscription (the latter is needed for role-assignment
  resources in `main.bicep`)
- `az` CLI ≥ 2.55 (`az upgrade`)
- Bicep CLI ≥ 0.30 — bundled with `az` (`az bicep upgrade`)
- Provider API keys: OpenAI, Anthropic, optionally Langfuse

## One-time setup per Azure subscription

```bash
# Pick subscription + tenant
az login
az account set --subscription <SUBSCRIPTION_ID>

# Register the resource providers movate uses. Idempotent.
az provider register --namespace Microsoft.ContainerRegistry
az provider register --namespace Microsoft.DBforPostgreSQL
az provider register --namespace Microsoft.KeyVault
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
```

## First-deploy per environment

The deployment has a chicken-and-egg: Container Apps reference Key
Vault secrets that don't exist until KV is provisioned. Two clean
paths:

### Option A: Two-pass deploy (recommended)

1. **Provision infra without Container Apps.** Comment out the
   `api` and `worker` modules in `main.bicep` for the first pass.
   Or run the deploy and accept the first attempt erroring on the
   missing secrets — Azure rolls back the failed pieces cleanly and
   the rest (KV, ACR, Postgres) sticks.

2. **Populate Key Vault secrets.**
   ```bash
   ENV=dev
   KV=movate-${ENV}-kv

   # Generate the Postgres admin password (or read from your password manager).
   PG_PW=$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-32)
   az keyvault secret set --vault-name $KV --name pg-admin-password --value "$PG_PW"

   # Provider API keys
   az keyvault secret set --vault-name $KV --name openai-api-key       --value "$OPENAI_API_KEY"
   az keyvault secret set --vault-name $KV --name anthropic-api-key    --value "$ANTHROPIC_API_KEY"

   # Langfuse (optional — leave empty strings if not using)
   az keyvault secret set --vault-name $KV --name langfuse-secret-key  --value "$LANGFUSE_SECRET_KEY"
   az keyvault secret set --vault-name $KV --name langfuse-public-key  --value "$LANGFUSE_PUBLIC_KEY"
   ```

3. **Build + push the image.** Tag with the version that matches
   `pyproject.toml`.
   ```bash
   ACR=movate${ENV}acr

   # Build in ACR (no local Docker needed):
   az acr build --registry $ACR --image movate:0.5.0 -f Dockerfile --target runtime .
   ```

4. **Run the full deployment.** Either re-enable the api/worker
   modules in `main.bicep` and re-run, or just re-run (it's
   idempotent — existing resources update in place).
   ```bash
   cp infra/azure/main.bicepparam.example main.${ENV}.bicepparam
   # Edit main.${ENV}.bicepparam: set env, image, postgresAdminPassword (KV ref).

   az group create -n movate-${ENV}-rg -l eastus2

   az deployment group create \
       -g movate-${ENV}-rg \
       -f infra/azure/main.bicep \
       -p main.${ENV}.bicepparam
   ```

### Option B: Bootstrap KV first

Create a "bootstrap" Key Vault in a separate resource group, populate
the secrets there, and use `az.getSecret(...)` in `main.bicepparam` to
pull from it during deployment. This is what the example
`bicepparam` file demonstrates. Slightly more setup; cleaner for
multi-env teams that want one auth surface.

## Verifying the deployment

```bash
# API health
API_URL=$(az deployment group show -g movate-${ENV}-rg -n main \
            --query properties.outputs.apiUrl.value -o tsv)
curl ${API_URL}/healthz
# → {"status":"ok","version":"0.5.0"}

# Logs (last 10 min, both api + worker)
az containerapp logs show -g movate-${ENV}-rg -n movate-${ENV}-api --tail 50
az containerapp logs show -g movate-${ENV}-rg -n movate-${ENV}-worker --tail 50

# Postgres reachable + schema applied (workers run init on boot)
PG_FQDN=$(az deployment group show -g movate-${ENV}-rg -n main \
            --query properties.outputs.postgresFqdn.value -o tsv)
psql "postgresql://movateadmin@${PG_FQDN}:5432/movate?sslmode=require" \
     -c "\dt"
# → expect: runs, failures, evals, workflow_runs, jobs, api_keys
```

## Mint the first API key

Once the API is up, mint a `mvt_live_...` key by running `movate auth
create-key` against the deployed DB. There are two ways:

```bash
# (a) From your laptop, with MOVATE_DB_URL pointing at the live DB:
PGPASSWORD=$(az keyvault secret show --vault-name movate-${ENV}-kv \
                --name pg-admin-password --query value -o tsv)
export MOVATE_DB_URL="postgresql://movateadmin:${PGPASSWORD}@${PG_FQDN}:5432/movate?sslmode=require"

movate auth create-key --tenant-id $(uuidgen | tr -d -) --env live --label first-key
# → prints the full mvt_live_... key on stdout; save it to your vault.

# (b) Or exec into the running API container:
az containerapp exec -g movate-${ENV}-rg -n movate-${ENV}-api \
    --command "movate auth create-key --tenant-id <uuid> --env live --label first-key"
```

## Update flow (incremental redeploy)

For routine updates after the first deploy:

```bash
# 1. Build + push new image
az acr build --registry movate${ENV}acr --image movate:0.5.1 -f Dockerfile --target runtime .

# 2. Update both Container Apps in-place
az containerapp update -g movate-${ENV}-rg -n movate-${ENV}-api    --image movate${ENV}acr.azurecr.io/movate:0.5.1
az containerapp update -g movate-${ENV}-rg -n movate-${ENV}-worker --image movate${ENV}acr.azurecr.io/movate:0.5.1
```

ACA does a rolling restart (no downtime if `minReplicas ≥ 2`).
`movate deploy` (lands stage 2 of v1.0) automates this.

## Tear down

```bash
az group delete -n movate-${ENV}-rg --yes --no-wait
```

Key Vault soft-delete keeps the vault recoverable for 90 days; the
purge-protection flag means you can't even force-delete it. If you
need to recreate `movate-${ENV}-kv` before 90d, change the env name
or pass `--purge-protection false` when re-creating (operator
intervention required).

## What this is NOT

- **NOT private-network.** The runtime is publicly addressable.
  VNet integration lands in v1.1 if a security review demands it.
  Auth still gates every endpoint (bearer-token middleware), so
  "publicly addressable" ≠ "publicly accessible."
- **NOT multi-region.** Single-region deployment. Customers needing
  failover should run their own active/passive across two of these.
- **NOT auto-deployed.** Stage 1 (this) provisions infra. Stage 2
  (`movate deploy` + `.github/workflows/deploy.yml`) builds the
  image and rolls out on `git push release/*`.
