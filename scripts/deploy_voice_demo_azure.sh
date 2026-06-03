#!/usr/bin/env bash
# Deploy the mdk-voice web demo to Azure Container Apps.
#
# What this does (idempotent — re-run to update the image or rotate secrets):
#   1. Create resource group + ACR + Container Apps environment, if missing.
#   2. Build the image *in the cloud* via `az acr build` (no local Docker
#      needed) from examples/web_demo/Dockerfile.
#   3. Push the provider API keys from your ~/.mdk_*_key files into Container
#      App secrets (chmod-600 files on your laptop → ACA-encrypted at rest).
#   4. Create / update the Container App with min/max replicas, external
#      ingress on port 8000, and the secrets mapped to env vars.
#   5. Print the public HTTPS URL — usable for the browser demo AND as the
#      Twilio voice URL (no ngrok needed once this is deployed).
#
# Usage:
#   ./scripts/deploy_azure.sh                       # uses defaults below
#   APP=my-demo MIN=0 ./scripts/deploy_azure.sh     # override
#
# Requires: az CLI logged in to the right subscription
# (`az account show` should print the right tenant/sub before you run this).

set -euo pipefail

# ── Tunables (override via env vars) ──────────────────────────────────────────
SUB="${SUB:-8fab0f8f-b577-45d7-a485-ec32f73b22be}"   # AZLABSV2.0-Sandbox(POC)
RG="${RG:-rg-mdk-voice-demo}"
LOCATION="${LOCATION:-eastus}"
ACR="${ACR:-crmdkvoicedemo$(echo -n "$SUB" | shasum | head -c 6)}"  # globally unique
ENV_NAME="${ENV_NAME:-cae-mdk-voice-demo}"
APP="${APP:-mdk-voice-demo}"
IMAGE="${IMAGE:-mdk-voice-demo:$(date +%Y%m%d-%H%M%S)}"
MIN="${MIN:-1}"   # min replicas (1 = always-warm, no cold-start; 0 = scale-to-zero)
MAX="${MAX:-3}"
CPU="${CPU:-0.5}"
MEMORY="${MEMORY:-1Gi}"
KEY_DIR="${KEY_DIR:-$HOME}"   # where the ~/.mdk_<provider>_key files live

# Colors for the human-skimmable log.
B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
step() { printf "\n${B}▸ %s${N}\n" "$*"; }
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }
warn() { printf "  ${Y}⚠${N} %s\n" "$*"; }
die()  { printf "  ${R}✗${N} %s\n" "$*" >&2; exit 1; }

# ── Sanity checks ─────────────────────────────────────────────────────────────
command -v az >/dev/null || die "az CLI not found. Install: brew install azure-cli"
[[ -f "$KEY_DIR/.mdk_openai_key"   ]] || die "missing $KEY_DIR/.mdk_openai_key"
[[ -f "$KEY_DIR/.mdk_cartesia_key" ]] || die "missing $KEY_DIR/.mdk_cartesia_key"
[[ -f "$KEY_DIR/.mdk_deepgram_key" ]] || die "missing $KEY_DIR/.mdk_deepgram_key"
[[ -f "$KEY_DIR/.mdk_lyzr_key"     ]] || warn "no Lyzr key — demo's Lyzr tier will fall back to OpenAI"

# Resolve script dir → repo root, regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKERFILE_REL="examples/web_demo/Dockerfile"
[[ -f "$REPO_ROOT/$DOCKERFILE_REL" ]] || die "Dockerfile not found at $REPO_ROOT/$DOCKERFILE_REL"

step "Targeting subscription"
az account set --subscription "$SUB"
az account show --query "{sub:name, id:id}" -o table

step "Ensure resource group ($RG / $LOCATION)"
az group create -n "$RG" -l "$LOCATION" -o none && ok "rg ready"

step "Ensure ACR ($ACR)"
if ! az acr show -n "$ACR" -g "$RG" >/dev/null 2>&1; then
    az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled true -o none
    ok "acr created"
else
    ok "acr exists"
fi

step "Build image in ACR (no local Docker required)"
# `az acr build` uploads the build context to ACR and runs the Dockerfile
# server-side. ~2-3 min the first time, then cached.
#
# IMPORTANT: build from a `git archive` tarball of TRACKED files only, NOT the
# working tree. `az acr build <dir>` WALKS the entire directory to evaluate
# .dockerignore per-file — and this repo can carry a multi-GB (observed: 200GB+)
# `.claude/worktrees/` tree of nested agent worktrees that .dockerignore excludes
# from the image but cannot stop the walker from traversing. That walk hangs the
# upload for many minutes. `git archive` sidesteps it: the tarball contains only
# committed files (a few MB), so the context is tiny and deterministic. The
# Dockerfile path inside the tar is repo-root-relative, same as before.
CONTEXT_TAR="$(mktemp -t mdk-voice-ctx-XXXXXX.tar.gz)"
trap 'rm -f "$CONTEXT_TAR"' EXIT
git -C "$REPO_ROOT" archive --format=tar.gz -o "$CONTEXT_TAR" HEAD
ok "build context: $(du -h "$CONTEXT_TAR" | cut -f1) tarball (tracked files only)"
az acr build \
    --registry "$ACR" \
    --image "$IMAGE" \
    --file "$DOCKERFILE_REL" \
    "$CONTEXT_TAR" \
    -o table
ok "image built: $ACR.azurecr.io/$IMAGE"

step "Ensure Container Apps environment ($ENV_NAME)"
if ! az containerapp env show -n "$ENV_NAME" -g "$RG" >/dev/null 2>&1; then
    az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOCATION" -o none
    ok "env created"
else
    ok "env exists"
fi

# ── Read keys into shell vars (NOT into the command line history) ────────────
read_key() { tr -d '\n\r' < "$1"; }
OPENAI_KEY="$(read_key "$KEY_DIR/.mdk_openai_key")"
CARTESIA_KEY="$(read_key "$KEY_DIR/.mdk_cartesia_key")"
DEEPGRAM_KEY="$(read_key "$KEY_DIR/.mdk_deepgram_key")"
LYZR_KEY="$(read_key "$KEY_DIR/.mdk_lyzr_key" 2>/dev/null || echo "")"

ACR_LOGIN_SERVER="$(az acr show -n "$ACR" -g "$RG" --query loginServer -o tsv)"
ACR_USER="$(az acr credential show -n "$ACR" --query username -o tsv)"
ACR_PASS="$(az acr credential show -n "$ACR" --query "passwords[0].value" -o tsv)"

# Build the secrets + env-var args. Secrets become encrypted in the app's
# revision template; the env-var side just references them by name.
SECRETS=(
    "openai-api-key=$OPENAI_KEY"
    "cartesia-api-key=$CARTESIA_KEY"
    "deepgram-api-key=$DEEPGRAM_KEY"
    "acr-pwd=$ACR_PASS"
)
ENVS=(
    "OPENAI_API_KEY=secretref:openai-api-key"
    "CARTESIA_API_KEY=secretref:cartesia-api-key"
    "DEEPGRAM_API_KEY=secretref:deepgram-api-key"
    "MDK_VOICE_DEMO_PUBLIC=1"
)
if [[ -n "$LYZR_KEY" ]]; then
    SECRETS+=("lyzr-api-key=$LYZR_KEY")
    ENVS+=("LYZR_API_KEY=secretref:lyzr-api-key")
fi

step "Deploying Container App ($APP)"
if az containerapp show -n "$APP" -g "$RG" >/dev/null 2>&1; then
    ok "app exists — updating image + secrets"
    # Rotate secrets (idempotent) then bump the image; ACA creates a new revision.
    az containerapp secret set -n "$APP" -g "$RG" --secrets "${SECRETS[@]}" -o none
    az containerapp update -n "$APP" -g "$RG" \
        --image "$ACR_LOGIN_SERVER/$IMAGE" \
        --set-env-vars "${ENVS[@]}" \
        --cpu "$CPU" --memory "$MEMORY" \
        --min-replicas "$MIN" --max-replicas "$MAX" \
        -o none
else
    az containerapp create -n "$APP" -g "$RG" \
        --environment "$ENV_NAME" \
        --image "$ACR_LOGIN_SERVER/$IMAGE" \
        --registry-server "$ACR_LOGIN_SERVER" \
        --registry-username "$ACR_USER" \
        --registry-password "$ACR_PASS" \
        --target-port 8000 \
        --ingress external \
        --transport auto \
        --cpu "$CPU" --memory "$MEMORY" \
        --min-replicas "$MIN" --max-replicas "$MAX" \
        --secrets "${SECRETS[@]}" \
        --env-vars "${ENVS[@]}" \
        -o none
    ok "app created"
fi

FQDN="$(az containerapp show -n "$APP" -g "$RG" --query "properties.configuration.ingress.fqdn" -o tsv)"

step "Done"
printf "\n${G}${B}▸ Public URL${N}\n  https://${FQDN}/\n"
printf "\n${B}▸ Share with the team${N}\n"
printf "  Browser demo:       https://${FQDN}/\n"
printf "  Twilio voice URL:   https://${FQDN}/twiml/voice  (no ngrok needed!)\n"
printf "  Twilio WS stream:   wss://${FQDN}/ws/twilio\n"
printf "\n${B}▸ Iterate${N}\n"
printf "  Re-run this script to rebuild + roll a new revision (zero-downtime).\n"
printf "  Logs:    az containerapp logs show -n ${APP} -g ${RG} --follow\n"
printf "  Scale 0: az containerapp update -n ${APP} -g ${RG} --min-replicas 0\n"
printf "\n"
