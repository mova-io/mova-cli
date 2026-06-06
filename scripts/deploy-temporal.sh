#!/usr/bin/env bash
# Deploy the self-hosted Temporal server + worker to Azure Container Apps
# (ADR 078 server, ADR 080 D1 worker + terminal-state sync). One command:
#   build image → redeploy with enableTemporal=true → bounce the server → validate.
#
# Usage (from a feat/temporal-durable-hitl checkout, repo root):
#   ./scripts/deploy-temporal.sh
#
# Override the dev defaults via env vars, e.g.:
#   RG=movate-prod-rg ACR=movateprodacr PARAMS=infra/azure/main.prod.bicepparam \
#     TEMPORAL_APP=movate-prod-temporal ./scripts/deploy-temporal.sh
#
# Prereqs: `az login` to the target subscription; the base movate stack (KV,
# Postgres, ACR, CAE) already provisioned; the pg-admin-password KV secret set.
#
# ⚠ PG-password footgun (see ADR 080 "real bug"): the redeploy re-runs the `pg`
# module, which RESETS the Postgres admin password to the `postgresAdminPassword`
# value in the bicepparam. That value MUST equal the `pg-admin-password` secret in
# Key Vault (the apps read the KV secret). If they drift, every app's PG auth
# breaks. Keep them in sync until the module is fixed to not overwrite on update.
set -euo pipefail

# --- config (override via env) ----------------------------------------------
RG="${RG:-movate-dev-rg}"
ACR="${ACR:-movatedevacrmvt}"
BICEP="${BICEP:-infra/azure/main.bicep}"
PARAMS="${PARAMS:-infra/azure/main.dev.bicepparam}"
TEMPORAL_APP="${TEMPORAL_APP:-movate-dev-temporal}"
# Unique, traceable image tag from git → forces fresh revisions every deploy
# (so apps re-read Key Vault secrets) and ties the running image to a commit.
TAG="${TAG:-0.7.0-temporal-$(git rev-parse --short HEAD 2>/dev/null || echo manual)}"
IMAGE="movate:${TAG}"

command -v az >/dev/null || { echo "✗ az CLI not found" >&2; exit 1; }
az account show >/dev/null 2>&1 || { echo "✗ not logged in — run 'az login'" >&2; exit 1; }
[ -f "$BICEP" ] || { echo "✗ $BICEP not found — run from the repo root" >&2; exit 1; }

echo "▸ Building ${IMAGE} in ${ACR} (this uploads the current checkout) ..."
az acr build -r "$ACR" -t "$IMAGE" -f Dockerfile .

echo "▸ Deploying ${BICEP} to ${RG} (enableTemporal=true, image=${IMAGE}) ..."
STATE=$(az deployment group create -g "$RG" -f "$BICEP" -p "$PARAMS" \
  --parameters enableTemporal=true image="$IMAGE" \
  --query "properties.provisioningState" -o tsv)
echo "  deployment: ${STATE}"
[ "$STATE" = "Succeeded" ] || { echo "✗ deployment did not succeed" >&2; exit 1; }

# The Temporal SERVER runs the public temporalio/auto-setup image, unchanged by
# the deploy, so it won't recycle on its own — force a fresh revision so it
# re-reads the (possibly updated) Key Vault password and retries schema setup.
SUFFIX="redeploy-$(date +%Y%m%d%H%M%S)"
echo "▸ Bouncing ${TEMPORAL_APP} → new revision (${SUFFIX}) ..."
az containerapp update -n "$TEMPORAL_APP" -g "$RG" --revision-suffix "$SUFFIX" -o none

cat <<EOF

✓ Deploy complete. Validate (give the server ~30–60s for schema setup):

  az containerapp logs show -n ${TEMPORAL_APP} -g ${RG} --tail 30
      → expect schema setup OK + frontend listening on :7233 (no PG auth errors)
  az containerapp logs show -n ${TEMPORAL_APP}-worker -g ${RG} --tail 30
      → expect "registered N workflows", no "No such option: --backend"
  az containerapp logs show -n movate-dev-api -g ${RG} --tail 30
      → expect no InvalidPasswordError

Then run a runtime:temporal workflow with a HUMAN node and confirm:
  GET  /api/v1/workflow-runs?status=paused   → the paused run appears
  POST /api/v1/workflow-runs/{id}/signal     → resolves to a SUCCESS terminal record
EOF
