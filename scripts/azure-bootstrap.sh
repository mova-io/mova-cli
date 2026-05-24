#!/usr/bin/env bash
# scripts/azure-bootstrap.sh — one-shot per-env Azure setup for movate-cli.
#
# Does the parts of the deploy onboarding that need az CLI superpowers
# (RG creation, service principal, federated credential, role
# assignments) — the stuff most likely to break on a typo. Idempotent:
# re-run after fixing a config or adding a new env without cleanup.
#
# What it does NOT do (humans + UI required):
#   * Get you an Azure subscription. Comes from your org's Azure admin.
#   * Set up GitHub Environment secrets. Prints what to paste at the end;
#     you paste them in the GitHub UI (Settings → Environments → <env>).
#   * Run the actual Bicep deploy. The chicken-and-egg with Key Vault
#     secrets means that's a human walk-through — see infra/azure/README.md.
#   * Mint the first runtime API key. Done after the Bicep deploy via
#     `az containerapp exec` — see step 6 of docs/azure-bootstrap.md.
#
# Usage:
#   scripts/azure-bootstrap.sh <env>
#   # e.g.  scripts/azure-bootstrap.sh dev
#
# Env vars (optional, sensible defaults):
#   AZURE_REGION       — default "eastus2"
#   ACR_NAME_OVERRIDE  — default "movate${env}acr" (ACR names are
#                        globally unique; override if there's a collision)
#   KV_NAME_OVERRIDE   — default "movate-${env}-kv" (Key Vault names are
#                        globally unique; override if you set nameSuffix
#                        in the bicepparam to dodge a collision)
#   GITHUB_REPO        — default "mova-io/mova-cli" (for the
#                        federated-credential subject)

set -euo pipefail

# ---------------------------------------------------------------------------
# Inputs + defaults
# ---------------------------------------------------------------------------

if [ $# -ne 1 ]; then
    echo "usage: $0 <env>     # e.g. $0 dev | staging | prod" >&2
    exit 2
fi

ENV="$1"
REGION="${AZURE_REGION:-eastus2}"
RG="movate-${ENV}-rg"
ACR="${ACR_NAME_OVERRIDE:-movate${ENV}acr}"
KV="${KV_NAME_OVERRIDE:-movate-${ENV}-kv}"
SP_NAME="movate-${ENV}-github-actions"
REPO="${GITHUB_REPO:-mova-io/mova-cli}"
FED_CRED_NAME="github-release-${ENV}"

# Colors that work everywhere (no ANSI on dumb terms; sentinel for `tput`).
if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    RED=$(tput setaf 1)
    BOLD=$(tput bold)
    DIM=$(tput dim)
    RESET=$(tput sgr0)
else
    GREEN=""; YELLOW=""; RED=""; BOLD=""; DIM=""; RESET=""
fi

ok()    { echo "${GREEN}✓${RESET} $*"; }
note()  { echo "${DIM}  $*${RESET}"; }
warn()  { echo "${YELLOW}!${RESET} $*"; }
die()   { echo "${RED}✗${RESET} $*" >&2; exit 1; }
header(){ echo; echo "${BOLD}$*${RESET}"; }

# ---------------------------------------------------------------------------
# Preflight: az CLI present + logged in + subscription set
# ---------------------------------------------------------------------------

header "Preflight"

command -v az >/dev/null 2>&1 || die "az CLI not on PATH. Install: https://learn.microsoft.com/cli/azure/install-azure-cli"
ok "az CLI installed ($(az version --query '"azure-cli"' -o tsv))"

SUB_ID=$(az account show --query id -o tsv 2>/dev/null) || \
    die "Not logged in. Run 'az login' first."
SUB_NAME=$(az account show --query name -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
ok "logged in to subscription: ${SUB_NAME} (${SUB_ID})"
note "tenant: ${TENANT_ID}"

# Sanity: confirm the operator actually intends this subscription. Catches
# the most common foot-gun (running against the wrong account).
read -r -p "Bootstrap env '${ENV}' against THIS subscription? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    die "aborted by user"
fi

# ---------------------------------------------------------------------------
# Resource group
# ---------------------------------------------------------------------------

header "Resource group: ${RG}"

if az group show -n "$RG" >/dev/null 2>&1; then
    ok "RG '${RG}' exists"
else
    az group create -n "$RG" -l "$REGION" -o none
    ok "created RG '${RG}' in ${REGION}"
fi

# ---------------------------------------------------------------------------
# Service principal for GitHub Actions OIDC federated auth
# ---------------------------------------------------------------------------

header "Service principal: ${SP_NAME}"

# Look up existing SP by display name. If multiple exist (rerun + AAD
# eventual-consistency window), `az ad sp list` returns N — take the
# first stable one.
APP_ID=$(az ad sp list --display-name "$SP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)

if [ -z "${APP_ID:-}" ]; then
    # `--skip-assignment` is the default in current az versions; we explicitly
    # avoid `--scopes`/`--role` here so we can assign granular roles below.
    APP_ID=$(az ad sp create-for-rbac --name "$SP_NAME" --query appId -o tsv)
    ok "created SP '${SP_NAME}' (appId=${APP_ID})"
    note "AAD propagation can take ~30s; the role assignments below will retry."
else
    ok "SP '${SP_NAME}' exists (appId=${APP_ID})"
fi

# ---------------------------------------------------------------------------
# Role assignments — Contributor on RG, AcrPush on ACR (once the
# Bicep deploy creates ACR; until then the assignment defers).
# ---------------------------------------------------------------------------

header "Role assignments"

# Contributor on the RG — sufficient for `az containerapp update`,
# `az acr build`, and reading deploy outputs. We add AcrPush separately
# because Contributor includes write-but-not-push on ACR (Container
# Registry's RBAC is stricter than RBAC-everywhere).
RG_SCOPE="/subscriptions/${SUB_ID}/resourceGroups/${RG}"

# Idempotent: `az role assignment create` errors if it already exists.
# Suppress the specific "already exists" failure; let other errors surface.
if az role assignment create \
        --assignee "$APP_ID" \
        --role "Contributor" \
        --scope "$RG_SCOPE" \
        -o none 2>/dev/null; then
    ok "Contributor on ${RG}"
else
    # Verify it actually exists vs propagation-failed
    if az role assignment list --assignee "$APP_ID" --scope "$RG_SCOPE" \
            --query "[?roleDefinitionName=='Contributor'] | length(@)" -o tsv \
            2>/dev/null | grep -q "^[1-9]"; then
        ok "Contributor on ${RG} (already existed)"
    else
        warn "Contributor assignment didn't take — retry in ~30s if a fresh SP"
    fi
fi

# AcrPush on the ACR (when it exists). Bicep creates the ACR; we defer
# the assignment if it doesn't exist yet but warn so the operator knows
# to come back.
ACR_SCOPE="${RG_SCOPE}/providers/Microsoft.ContainerRegistry/registries/${ACR}"
if az acr show -n "$ACR" --resource-group "$RG" >/dev/null 2>&1; then
    if az role assignment create \
            --assignee "$APP_ID" \
            --role "AcrPush" \
            --scope "$ACR_SCOPE" \
            -o none 2>/dev/null; then
        ok "AcrPush on ${ACR}"
    elif az role assignment list --assignee "$APP_ID" --scope "$ACR_SCOPE" \
            --query "[?roleDefinitionName=='AcrPush'] | length(@)" -o tsv \
            2>/dev/null | grep -q "^[1-9]"; then
        ok "AcrPush on ${ACR} (already existed)"
    else
        warn "AcrPush assignment didn't take — retry in ~30s if a fresh SP"
    fi
else
    warn "ACR '${ACR}' doesn't exist yet — assign AcrPush after Bicep deploy:"
    note "  az role assignment create --assignee ${APP_ID} --role AcrPush --scope ${ACR_SCOPE}"
fi

# Key Vault Secrets Officer on the KV (when it exists). The operator (this
# SP, or the human running the two-pass deploy) must SET the KV secrets
# between passes; on an RBAC-mode Key Vault that requires a data-plane
# role — without it, `az keyvault secret set` fails with ForbiddenByRbac.
# Same defer-with-warning shape as AcrPush: Bicep creates the KV on the
# first pass, so re-run this script after that pass to lock the grant in.
KV_SCOPE="${RG_SCOPE}/providers/Microsoft.KeyVault/vaults/${KV}"
if az keyvault show -n "$KV" --resource-group "$RG" >/dev/null 2>&1; then
    if az role assignment create \
            --assignee "$APP_ID" \
            --role "Key Vault Secrets Officer" \
            --scope "$KV_SCOPE" \
            -o none 2>/dev/null; then
        ok "Key Vault Secrets Officer on ${KV}"
    elif az role assignment list --assignee "$APP_ID" --scope "$KV_SCOPE" \
            --query "[?roleDefinitionName=='Key Vault Secrets Officer'] | length(@)" -o tsv \
            2>/dev/null | grep -q "^[1-9]"; then
        ok "Key Vault Secrets Officer on ${KV} (already existed)"
    else
        warn "Key Vault Secrets Officer assignment didn't take — retry in ~30s if a fresh SP"
    fi
else
    warn "Key Vault '${KV}' doesn't exist yet — assign Secrets Officer after Bicep deploy:"
    note "  az role assignment create --assignee ${APP_ID} --role 'Key Vault Secrets Officer' --scope ${KV_SCOPE}"
fi

# ---------------------------------------------------------------------------
# Federated credential — pins the SP to a specific GitHub branch
# ---------------------------------------------------------------------------

header "Federated credential: ${FED_CRED_NAME}"

# Subject must EXACTLY match what GitHub Actions sends: the workflow's
# `permissions: id-token: write` produces a JWT with this subject when
# the workflow runs on `release/${ENV}`.
SUBJECT="repo:${REPO}:ref:refs/heads/release/${ENV}"

# Check if it already exists (by name).
if az ad app federated-credential list --id "$APP_ID" \
        --query "[?name=='${FED_CRED_NAME}'] | length(@)" -o tsv 2>/dev/null \
        | grep -q "^[1-9]"; then
    ok "federated credential '${FED_CRED_NAME}' already exists"
    note "subject: ${SUBJECT}"
else
    az ad app federated-credential create --id "$APP_ID" --parameters "$(cat <<EOF
{
    "name": "${FED_CRED_NAME}",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "${SUBJECT}",
    "audiences": ["api://AzureADTokenExchange"]
}
EOF
)" -o none
    ok "created federated credential '${FED_CRED_NAME}'"
    note "subject: ${SUBJECT}"
fi

# ---------------------------------------------------------------------------
# Wrap up: print the values that go into GitHub Environment secrets
# ---------------------------------------------------------------------------

header "Done. Next: paste these into GitHub Environment '${ENV}'"

cat <<EOF

  Settings → Environments → ${ENV} → Environment secrets

  ${BOLD}AZURE_CLIENT_ID${RESET}        ${APP_ID}
  ${BOLD}AZURE_TENANT_ID${RESET}        ${TENANT_ID}
  ${BOLD}AZURE_SUBSCRIPTION_ID${RESET}  ${SUB_ID}
  ${BOLD}AZURE_RG${RESET}               ${RG}
  ${BOLD}AZURE_ACR${RESET}              ${ACR}

  ${DIM}# After Bicep deploys the infra:${RESET}
  ${BOLD}RUNTIME_URL${RESET}            (output of the Bicep deploy; the ACA API FQDN)
  ${BOLD}RUNTIME_KEY${RESET}            (output of \`movate auth create-key\` against the deployed DB)

EOF

header "Next steps"
cat <<EOF
  1. Run the Bicep deploy:
       cp infra/azure/main.bicepparam.example infra/azure/main.${ENV}.bicepparam
       \$EDITOR infra/azure/main.${ENV}.bicepparam   # set env, postgresAdminPassword, etc.
       az deployment group create -g ${RG} \\
           -f infra/azure/main.bicep \\
           -p infra/azure/main.${ENV}.bicepparam

  2. Mint the first runtime key:
       az containerapp exec -g ${RG} -n movate-${ENV}-api \\
           --command "movate auth create-key --tenant-id \$(uuidgen) --env live --label bootstrap"
     Save the mvt_live_... value as the RUNTIME_KEY secret above.

  3. If ACR / Key Vault didn't exist when this script ran, re-run this
     script after the first Bicep pass to lock in the deferred grants
     (AcrPush on ACR, Key Vault Secrets Officer on KV), or assign now:
       az role assignment create --assignee ${APP_ID} --role AcrPush \\
           --scope ${ACR_SCOPE}
       az role assignment create --assignee ${APP_ID} \\
           --role 'Key Vault Secrets Officer' --scope ${KV_SCOPE}

  4. Then test the deploy path locally:
       export MOVATE_${ENV^^}_KEY="<the mvt_live_... key>"
       movate config add-target ${ENV} \\
           --url <RUNTIME_URL> --key-env MOVATE_${ENV^^}_KEY \\
           --azure-subscription ${SUB_ID} \\
           --azure-resource-group ${RG} \\
           --azure-acr ${ACR} \\
           --azure-env ${ENV} \\
           --set-active
       movate doctor --target ${ENV}     # validate the wiring
       movate deploy --target ${ENV} --dry-run
       movate deploy --target ${ENV}

  5. And then auto-deploy from CI:
       git checkout -b release/${ENV} && git push -u origin release/${ENV}
EOF
