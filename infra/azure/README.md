# Azure deployment — operator walkthrough

End-to-end recipe for getting movate running on Azure. Covers a fresh
deployment from zero; for incremental updates (new image, scale
changes), skip to the [Update flow](#update-flow) section.

**For the abbreviated runbook with the automated identity + RG
bootstrap, see [docs/azure-bootstrap.md](../../docs/azure-bootstrap.md)
— this README is the Bicep deep-dive; the bootstrap doc is the
8-step "from zero to auto-deploy" path.**

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

   # KEDA Postgres scaler connection string — the worker's autoscaler
   # lives OUTSIDE the worker container (in the ACA env's KEDA
   # sidecar), so it needs a self-contained DSN to count queued jobs.
   # Substitute the FQDN/db that the first-pass Bicep deploy created.
   PG_FQDN=$(az postgres flexible-server show -g movate-${ENV}-rg \
       -n movate-${ENV}-pg --query fullyQualifiedDomainName -o tsv)
   az keyvault secret set --vault-name $KV --name pg-connection-string \
       --value "host=$PG_FQDN port=5432 user=movate password=$PG_PW dbname=movate sslmode=require"

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

### Self-hosted Langfuse (optional)

By default tracing goes to Langfuse Cloud (the api/worker just carry the
`langfuse-public-key` / `langfuse-secret-key`). To self-host Langfuse v2 on
Azure instead — backed by a `langfuse` database on the *same* Postgres
Flexible Server — set `deployLangfuse = true` in your `.bicepparam` and
populate four extra Key Vault secrets BEFORE the deploy:

```bash
ENV=dev; KV=movate-${ENV}-kv
PG_FQDN=$(az postgres flexible-server show -g movate-${ENV}-rg \
    -n movate-${ENV}-pg --query fullyQualifiedDomainName -o tsv)
# Reuse the Postgres admin password from the pg-admin-password secret.
PG_PW=$(az keyvault secret show --vault-name $KV --name pg-admin-password --query value -o tsv)

# Full connection string pointing at the `langfuse` database (Bicep creates
# it when deployLangfuse=true). ACA can't interpolate a secret into a value,
# so Langfuse's DATABASE_URL must be a single secret.
az keyvault secret set --vault-name $KV --name langfuse-database-url \
    --value "postgresql://movateadmin:${PG_PW}@${PG_FQDN}:5432/langfuse?sslmode=require"
az keyvault secret set --vault-name $KV --name langfuse-nextauth-secret --value "$(openssl rand -base64 32)"
az keyvault secret set --vault-name $KV --name langfuse-salt            --value "$(openssl rand -base64 32)"
az keyvault secret set --vault-name $KV --name langfuse-encryption-key  --value "$(openssl rand -hex 32)"
```

Then deploy. Langfuse runs its own DB migrations at boot. Afterwards:

1. Open the `langfuseUrl` deployment output, sign up (first user becomes
   admin), create an org + project.
2. Mint project API keys and store them so the movate apps pick them up:
   ```bash
   az keyvault secret set --vault-name $KV --name langfuse-public-key --value "pk-lf-..."
   az keyvault secret set --vault-name $KV --name langfuse-secret-key --value "sk-lf-..."
   ```
3. Re-deploy (or `az containerapp revision restart`) so the api/worker pick
   up the new keys. `LANGFUSE_HOST` is already pointed at the self-hosted
   URL automatically when `deployLangfuse=true`.

To tighten signup after your admin account exists, set `disableSignup=true`
on the langfuse module (param surfaced for a follow-up `.bicepparam` knob).

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

The Container App's Bicep references a Key Vault secret called
`bootstrap-api-key` and surfaces it as the `MDK_SEED_API_KEY` env
var. On every pod start, the runtime's `_seed_bootstrap_key()` reads
that value and idempotently inserts the matching row into the
`api_keys` table. This is the recommended path because the key
survives revision recycles AND is the same value the operator stores
locally — no copy-paste, no `az containerapp exec` required.

```bash
# Recommended: one-line mint + Key Vault upload + local save.
# Run this once per environment, before the first `mdk deploy`.
mdk auth bootstrap-seed ${ENV} --keyvault movate-${ENV}-kv${SFX:-}

# To rotate the bootstrap key later (security event, etc.):
mdk auth bootstrap-seed ${ENV} --keyvault movate-${ENV}-kv${SFX:-} --force
# Then restart the Container App so it re-seeds:
az containerapp revision restart -g movate-${ENV}-rg -n movate-${ENV}-api \
    --revision $(az containerapp show -g movate-${ENV}-rg -n movate-${ENV}-api \
                   --query properties.latestRevisionName -o tsv)
```

If you'd rather mint tenant-scoped keys by hand (legacy flow, doesn't
benefit from the seed key auto-reseed):

```bash
# (a) From your laptop, with MDK_DB_URL pointing at the live DB:
PGPASSWORD=$(az keyvault secret show --vault-name movate-${ENV}-kv \
                --name pg-admin-password --query value -o tsv)
export MDK_DB_URL="postgresql://movateadmin:${PGPASSWORD}@${PG_FQDN}:5432/movate?sslmode=require"

movate auth create-key --tenant-id $(uuidgen | tr -d -) --env live --label first-key
# → prints the full mvt_live_... key on stdout; save it to your vault.

# (b) Or exec into the running API container:
az containerapp exec -g movate-${ENV}-rg -n movate-${ENV}-api \
    --command "movate auth create-key --tenant-id <uuid> --env live --label first-key"
```

## Update flow (incremental redeploy)

For routine updates after the first deploy, use `movate deploy` —
it wraps the same `az acr build` + `az containerapp update` sequence
plus a `/healthz` poll that verifies the new revision is actually
serving before returning.

```bash
# One-time: register this RG/ACR/env combo as a deploy target so
# `movate deploy` knows where to push.
movate config add-target ${ENV} \
    --url $API_URL \
    --key-env MDK_${ENV^^}_KEY \
    --azure-subscription $(az account show --query id -o tsv) \
    --azure-resource-group movate-${ENV}-rg \
    --azure-acr movate${ENV}acr \
    --azure-env ${ENV} \
    --set-active

# Every subsequent update is one command. Image tag defaults to
# movate:<version>-<git-sha-short> so each deploy is traceable.
movate deploy --target ${ENV}
```

ACA does a rolling restart (no downtime if `minReplicas ≥ 2`).

If you'd rather drive the raw `az` commands manually:

```bash
# 1. Build + push new image
az acr build --registry movate${ENV}acr --image movate:0.5.1 -f Dockerfile --target runtime .

# 2. Update both Container Apps in-place
az containerapp update -g movate-${ENV}-rg -n movate-${ENV}-api    --image movate${ENV}acr.azurecr.io/movate:0.5.1
az containerapp update -g movate-${ENV}-rg -n movate-${ENV}-worker --image movate${ENV}acr.azurecr.io/movate:0.5.1
```

For CI, push to a `release/<env>` branch and
[.github/workflows/deploy.yml](../../.github/workflows/deploy.yml) does
the same flow via Azure federated OIDC — no client secrets in
GitHub. See the workflow file for the per-env secret list it expects.

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
- **NOT auto-deployed by Bicep alone.** Stage 1 (the Bicep here)
  provisions infra. Stage 2 — `movate deploy` and
  [.github/workflows/deploy.yml](../../.github/workflows/deploy.yml)
  — builds the image and rolls it out (manually with `movate deploy`,
  or automatically on `git push release/*`).
