# Azure bootstrap — operator runbook

End-to-end walkthrough from "you have an Azure subscription" to "`git
push release/<env>` auto-deploys to ACA." Eight steps; about an hour of
real work, mostly waiting on Postgres provisioning.

The first run for any environment is the longest — `dev`, `staging`,
and `prod` each take a fresh run-through since they need their own
resource group, ACR, service principal, and GitHub Environment.

## What this runbook automates vs. what it doesn't

| Step | Automated? | Why |
|---|---|---|
| 1. Get an Azure subscription | No (your Azure admin) | Org-level decision |
| 2. `az login` + pick subscription | No (5-second user action) | Per-machine credential |
| 3. Create RG + SP + federated cred | **Yes** — `scripts/azure-bootstrap.sh` | Most error-prone manual step |
| 4. Bicep deploy (infra) | Partial (the two-pass dance) | Key-Vault chicken-and-egg |
| 5. Mint first runtime API key | Manual (`az containerapp exec`) | Happens once per env |
| 6. Add GitHub Environment secrets | No (UI) | GitHub doesn't expose CLI for env secrets |
| 7. First `mdk deploy` locally | **Yes** — one command | Code is the point |
| 8. Auto-deploy via `release/*` push | **Yes** — `git push` | Code is the point |

## 1. Subscription + permissions

You need:

- An Azure subscription Movate engineers can deploy into (could be
  separate dev / staging / prod subs, or one shared sub with
  separate resource groups — both fine).
- Your account having **Contributor** on the target RG, or **Owner**
  on the subscription if you'll be creating the RG yourself.
- **User Access Administrator** on the subscription if you'll be
  setting up the service principal in step 3. Otherwise, ask your
  Azure AD admin to run that script.

If you're not sure: run `az login`, then `az account list -o table`.
The "Default" subscription is the one that'll be targeted.

## 2. Login + subscription

```bash
az login
az account set --subscription "<your-subscription-id>"
az account show     # confirm the right sub before continuing
```

## 3. Bootstrap identity + RG (the painful part, automated)

```bash
scripts/azure-bootstrap.sh <env>   # e.g. dev, staging, prod
```

The script is idempotent — rerun freely after fixing a typo or to
re-print the GitHub secrets list. It:

- Creates `movate-<env>-rg` if missing (in `eastus2` by default;
  override with `AZURE_REGION=westus2 scripts/azure-bootstrap.sh dev`).
- Creates a service principal `movate-<env>-github-actions` if
  missing (no-op if it already exists from a prior run).
- Assigns **Contributor** on the RG and **AcrPush** on the ACR
  (deferred with a warning if ACR doesn't exist yet — Bicep creates
  it in step 4; you re-run step 3 after step 4 to lock in AcrPush).
- Creates the **federated credential** pinning the SP to
  `refs/heads/release/<env>`, so GitHub Actions can exchange its
  OIDC token for an Azure access token. No client secrets stored
  anywhere.
- Prints the values to paste into the GitHub Environment in step 6.

Save the printed `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, etc. — you'll
need them in step 6.

## 4. Bicep deploy (the infra)

This is the chicken-and-egg part. Container Apps reference Key Vault
secrets that have to exist at deploy time, but Key Vault is itself
created by the Bicep deployment. Two strategies:

### Option A: Two-pass deploy (recommended)

First pass with `enableApiWorker: false`:

```bash
cp infra/azure/main.bicepparam.example infra/azure/main.<env>.bicepparam
$EDITOR infra/azure/main.<env>.bicepparam    # set env, postgresAdminPassword, ...
# In the param file, set: param enableApiWorker = false

az deployment group create \
    -g movate-<env>-rg \
    -f infra/azure/main.bicep \
    -p infra/azure/main.<env>.bicepparam
```

This deploys Log Analytics + ACR + KV + Postgres + ACA env, but skips
the API + worker apps. Takes ~15-25 min (Postgres is the slow one).

Now populate the KV secrets the apps will reference:

```bash
KV=movate-<env>-kv

# Postgres admin password (matches main.<env>.bicepparam)
az keyvault secret set --vault-name $KV --name pg-admin-password --value "$PG_PW"

# KEDA Postgres scaler connection string — the worker's autoscaler
# lives in ACA's KEDA sidecar, NOT in the worker container, so it
# needs a self-contained DSN to count queued jobs. Without this
# secret the worker will deploy but won't autoscale.
PG_FQDN=$(az postgres flexible-server show -g movate-<env>-rg \
    -n movate-<env>-pg --query fullyQualifiedDomainName -o tsv)
az keyvault secret set --vault-name $KV --name pg-connection-string \
    --value "host=$PG_FQDN port=5432 user=movate password=$PG_PW dbname=movate sslmode=require"

# Provider keys (and anything else the runtime needs at boot)
az keyvault secret set --vault-name $KV --name openai-api-key --value "sk-..."
# ... repeat per secret your deployment references
```

Then flip `enableApiWorker = true` in the param file and re-run
`az deployment group create`. The api + worker apps come up reading
from KV via their managed identities.

### Option B: Bootstrap a placeholder secret first

If you prefer one Bicep run, pre-create the KV outside of Bicep, push
the secrets, then run Bicep referencing the pre-existing KV. The
`infra/azure/main.bicepparam.example` has a comment block on this.

Either way, end state: ACA environment + API + worker apps running.

After this step, re-run `scripts/azure-bootstrap.sh <env>` once more
to assign **AcrPush** to the SP (it was deferred because ACR didn't
exist on the first pass).

## 5. Mint the first runtime API key

`mdk auth create-key` runs against whichever storage backend the
host can reach. The simplest path: exec into the running API container,
which already has Postgres credentials wired up by Bicep.

```bash
ENV=dev   # or whichever
az containerapp exec -g movate-${ENV}-rg -n movate-${ENV}-api \
    --command "mdk auth create-key --tenant-id $(uuidgen) --env live --label bootstrap"
```

Copy the `mvt_live_...` value — that's the `RUNTIME_KEY` for step 6
and the value you'll export locally to call the deployed runtime.

**This key is shown once and never again.** Save it in your password
manager / 1Password CLI / `pass` immediately.

## 6. GitHub Environment secrets

GitHub → Settings → Environments → New environment → name it `dev` /
`staging` / `prod`. (Use the same names you've been using throughout —
they must match the branch name pattern `release/<env>`.)

Add these secrets (from step 3's output + the values from steps 4 & 5):

| Secret | Source |
|---|---|
| `AZURE_CLIENT_ID` | step 3 output |
| `AZURE_TENANT_ID` | step 3 output |
| `AZURE_SUBSCRIPTION_ID` | step 3 output |
| `AZURE_RG` | step 3 output |
| `AZURE_ACR` | step 3 output |
| `RUNTIME_URL` | output of step 4's deploy (the API app FQDN) |
| `RUNTIME_KEY` | step 5's `mvt_live_...` |

For `prod`, also configure **required reviewers** under the
Environment's "Deployment protection rules" — production deploys
should require approval.

## 7. First `mdk deploy` locally (smoke the path)

Before letting CI deploy, do one manual deploy to catch any IAM /
config / image-build issues in the loop you're standing in:

```bash
export MOVATE_DEV_KEY="<the mvt_live_... value from step 5>"

mdk config add-target dev \
    --url <RUNTIME_URL> \
    --key-env MOVATE_DEV_KEY \
    --azure-subscription "$AZURE_SUBSCRIPTION_ID" \
    --azure-resource-group movate-dev-rg \
    --azure-acr movatedevacr \
    --azure-env dev \
    --set-active

# Validate every piece of the deploy path before pushing buttons:
mdk doctor --target dev

# Then plan + execute:
mdk deploy --target dev --dry-run
mdk deploy --target dev
```

`mdk doctor --target dev` walks `az login → subscription → RG →
ACR → containerapp api → containerapp worker → /healthz`. If
anything's red, fix it before running `mdk deploy` — the deploy
errors are downstream of these.

## 8. Auto-deploy via `release/<env>` push

The CI workflow at [.github/workflows/deploy.yml](../.github/workflows/deploy.yml)
triggers on push to `release/<env>`. With steps 3 + 6 done, this just
works:

```bash
git checkout -b release/dev
git push -u origin release/dev
```

Watch the workflow in the Actions tab. The flow:

1. Resolves the env name from the branch (`release/dev` → `dev`).
2. Azure federated OIDC login (no stored secrets).
3. Hydrates `~/.movate/config.yaml` from the GitHub Environment
   secrets you set in step 6.
4. Runs `mdk deploy --target dev` end-to-end.

For ad-hoc deploys (e.g. emergency rollback), use the
**workflow_dispatch** trigger from the Actions UI with the
`target_env` input.

## Troubleshooting

| Symptom | Most likely cause | Fix |
|---|---|---|
| `mdk deploy` exits 2 with "azure subscription missing" | Target wasn't registered with `--azure-*` flags | Re-run `mdk config add-target` |
| `mdk deploy` says `/healthz` timed out (exit 124) | ACA revision is still rolling out, or the new image crashed | `az containerapp logs show -g ... -n movate-<env>-api --tail 100` |
| `az acr build` says "AcrPush not granted" | Stage 3 deferred AcrPush because ACR didn't exist yet | Re-run `ACR_NAME_OVERRIDE=<actual-acr-name> scripts/azure-bootstrap.sh <env>` |
| Bicep deploy fails with `VaultAlreadyExists` / `AlreadyInUse` / `ServerNameAlreadyExists` | Default resource names (`movate-<env>-kv`, `movate<env>acr`, `movate-<env>-pg`) collide with another Azure tenant — these live in Azure-wide global namespaces | Set `nameSuffix = '<2-6-char-tag>'` in your bicepparam (e.g. your initials, org slug). The Bicep template appends it to KV / ACR / Postgres names. Then re-run the deploy with the new ACR name: `ACR_NAME_OVERRIDE=movate<env>acr<suffix> scripts/azure-bootstrap.sh <env>` for the SP's AcrPush role. |
| Bicep deploy fails: `ResourceProviderNotRegistered` for `Microsoft.App` | First time deploying ACA in this subscription | `az provider register -n Microsoft.App --wait` (takes 1-15 min) then re-run the deploy |
| Container Apps stuck in `provisioningState: InProgress`, `/healthz` returns "Container App stopped" 404 page | Historical: ACA's revision creation could deadlock when the apps' system-assigned managed identities didn't yet have AcrPull on the ACR or KV Secrets User on the KV. **Fixed in the current Bicep** by switching to user-assigned managed identities created at the `main.bicep` top level — role assignments land before the apps exist. If you DID hit this on an old deploy, the workaround is: out-of-band `az role assignment create --assignee <mi-principalId> --role AcrPull --scope <acr-id>` (same for KV with `Key Vault Secrets User`), then `az containerapp update -g <rg> -n movate-<env>-api --revision-suffix retry1`. Re-deploy with the current Bicep to permanently move to UAIs. |
| GH Actions deploy: "AADSTS70021: No matching federated identity record" | Branch name doesn't match `release/<env>` pattern | Check the branch name, or re-create the federated credential with the right `subject` |
| `mdk doctor --target prod` shows "subscription match: missing" | Logged in to a different sub locally | `az account set --subscription <id>` |
| GH workflow can't find the Environment | Environment name doesn't match the branch suffix | Create / rename the GH Environment to match `release/<env>` |

## Cost expectations

Steady-state idle cost per env (no traffic, min replicas = 0/0):

- **dev** — ~$50-100/mo. Postgres `Standard_B1ms` is the bulk
  (~$25/mo); ACR Basic ($5/mo); Log Analytics minimum (~$5/mo);
  ACA scales to zero when idle.
- **staging** — ~$100-200/mo. Postgres bumps to `Standard_B2s`.
- **prod** — ~$300-500/mo. Postgres `Standard_D2ds_v4` (~$120/mo);
  ACA min replicas typically 2 to avoid cold-start latency
  (~$70/mo); ACR Standard ($20/mo); Log Analytics with longer
  retention.

These are bounds; actual cost scales with traffic. Confirm budget
with your finance lead before deploying prod.

## What's NOT covered

- **Multi-region failover** — single-region deployment; bring your
  own active/passive across two of these.
- **Custom domain + TLS** — ACA supports it; add a `customDomains`
  block to the API app's Bicep module when you need it.
- **VNet integration** — public ingress in v1.0; auth still gates
  every endpoint. Move to VNet in v1.1 if a security review
  demands it.

See [BACKLOG.md](../BACKLOG.md) for the post-v1.0 roadmap (KEDA
queue-depth scaler, job retry policy, rate limiting, etc.).
