#!/usr/bin/env bash
# One-time setup: provision Azure AD app + federated credential + RBAC so
# the GitHub Actions deploy.yml workflow can authenticate via OIDC and roll
# a new ACA revision on every push to main — without a long-lived secret
# in the repo.
#
# Safe to re-run; every step is idempotent.
#
# Prereqs:
#   - az CLI logged in with rights to create AAD apps in your tenant
#     (Application Administrator OR Cloud Application Administrator OR
#     owner of an existing target sub)
#   - gh CLI logged in with `repo` scope on this repo (gh auth login)

set -euo pipefail

SUB="${SUB:-8fab0f8f-b577-45d7-a485-ec32f73b22be}"   # AZLABSV2.0-Sandbox(POC)
RG="${RG:-rg-mdk-voice-demo}"
APP_NAME="${APP_NAME:-mdk-voice-demo}"
ACR_NAME="${ACR_NAME:-crmdkvoicedemobadf3e}"
GH_REPO="${GH_REPO:-mova-io/mdk-voice}"
AAD_APP_NAME="${AAD_APP_NAME:-mdk-voice-demo-gh-actions}"

B=$'\e[1m'; G=$'\e[32m'; N=$'\e[0m'
step() { printf "\n${B}▸ %s${N}\n" "$*"; }
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }

command -v az >/dev/null || { echo "install az CLI (brew install azure-cli)"; exit 1; }
command -v gh >/dev/null || { echo "install gh CLI (brew install gh)"; exit 1; }

az account set --subscription "$SUB" >/dev/null
TENANT_ID=$(az account show --query tenantId -o tsv)

step "Ensure Azure AD app exists ($AAD_APP_NAME)"
APP_ID=$(az ad app list --display-name "$AAD_APP_NAME" --query "[0].appId" -o tsv)
if [[ -z "$APP_ID" ]]; then
    APP_ID=$(az ad app create --display-name "$AAD_APP_NAME" --query appId -o tsv)
    ok "AAD app created: $APP_ID"
else
    ok "AAD app exists: $APP_ID"
fi

step "Ensure service principal exists for the app"
SP_ID=$(az ad sp list --filter "appId eq '$APP_ID'" --query "[0].id" -o tsv)
if [[ -z "$SP_ID" ]]; then
    az ad sp create --id "$APP_ID" -o none
    SP_ID=$(az ad sp list --filter "appId eq '$APP_ID'" --query "[0].id" -o tsv)
fi
ok "service principal: $SP_ID"

step "Grant Contributor on RG ($RG)"
# Idempotent — az role assignment create returns the existing if it's already there.
az role assignment create \
    --assignee "$APP_ID" \
    --role Contributor \
    --scope "/subscriptions/$SUB/resourceGroups/$RG" \
    >/dev/null 2>&1 || true
ok "Contributor granted on /subscriptions/$SUB/resourceGroups/$RG"

step "Grant AcrPush on the registry ($ACR_NAME) so the runner can build images"
ACR_ID=$(az acr show -n "$ACR_NAME" -g "$RG" --query id -o tsv)
az role assignment create \
    --assignee "$APP_ID" \
    --role AcrPush \
    --scope "$ACR_ID" \
    >/dev/null 2>&1 || true
ok "AcrPush granted on $ACR_NAME"

step "Federate GitHub OIDC trust for $GH_REPO (branch: main)"
# The :ref:refs/heads/main subject ties this credential to *only* commits on
# the main branch of the repo — no other workflow can mint a token with this app.
SUBJECT="repo:${GH_REPO}:ref:refs/heads/main"
EXISTING=$(az ad app federated-credential list --id "$APP_ID" \
    --query "[?subject=='$SUBJECT'].id" -o tsv)
if [[ -z "$EXISTING" ]]; then
    az ad app federated-credential create --id "$APP_ID" --parameters "$(cat <<EOF
{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "${SUBJECT}",
  "audiences": ["api://AzureADTokenExchange"]
}
EOF
)" -o none
    ok "federated credential created for main"
else
    ok "federated credential already exists for main"
fi

step "Set GitHub repository secrets"
gh secret set AZURE_CLIENT_ID       --repo "$GH_REPO" --body "$APP_ID"
gh secret set AZURE_TENANT_ID       --repo "$GH_REPO" --body "$TENANT_ID"
gh secret set AZURE_SUBSCRIPTION_ID --repo "$GH_REPO" --body "$SUB"
ok "secrets written"

step "Done"
cat <<EOF

Next push to main on $GH_REPO will:
  1. Run ci.yml (lint + types + tests).
  2. On success, fire deploy.yml: az acr build + az containerapp update.
  3. Smoke-test the new revision.

You can also trigger a deploy manually:
  gh workflow run deploy.yml --repo $GH_REPO

To rotate / revoke, delete the federated credential:
  az ad app federated-credential delete --id $APP_ID --federated-credential-id github-main
EOF
