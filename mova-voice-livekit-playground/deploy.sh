#!/usr/bin/env bash
# Build + deploy the Mova-iO Voice Playground (LiveKit) — the thin proxy in front
# of Lyzr's hosted voice service (voice-livekit.studio.lyzr.ai) — to its own
# Azure Container App. Creates the app on first run, updates it thereafter.
#
# Unlike mova-voice-playground/deploy.sh, the build context is THIS directory
# (no movate.voice install needed — the agent runs on LiveKit Cloud).
#
# Usage:
#   LYZR_API_KEY=sk-... mova-voice-livekit-playground/deploy.sh
#   TAG=mytag LYZR_API_KEY=sk-... mova-voice-livekit-playground/deploy.sh
#   SKIP_BUILD=1 TAG=<existing> LYZR_API_KEY=sk-... mova-voice-livekit-playground/deploy.sh
set -euo pipefail

ACR="${ACR:-movatedevacrmvt}"
RG="${RG:-movate-dev-rg}"
ENVIRONMENT="${ENVIRONMENT:-movate-dev-cae}"
APP="${APP:-mova-voice-livekit-playground}"
IMAGE="${IMAGE:-mova-voice-livekit-playground}"
PORT="${PORT:-8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

SHA="$(git -C "${ROOT}" rev-parse --short HEAD)"
TAG="${TAG:-vlk-${SHA}}"
REF="${ACR}.azurecr.io/${IMAGE}:${TAG}"

# The Lyzr key the proxy uses server-side. Falls back to the shared public demo
# key baked into server.py if unset — set it here for a real deploy.
LYZR_API_KEY="${LYZR_API_KEY:-}"
LYZR_DEFAULT_AGENT_ID="${LYZR_DEFAULT_AGENT_ID:-6a26e4fc6d80be4fdfe65fa1}"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  echo "→ building ${REF} (context: ${HERE})"
  az acr build -r "${ACR}" -t "${IMAGE}:${TAG}" -f "${HERE}/Dockerfile" "${HERE}"
fi

ENV_ARGS=("PORT=${PORT}" "BUILD_TAG=${TAG}" "LYZR_DEFAULT_AGENT_ID=${LYZR_DEFAULT_AGENT_ID}")
[[ -n "${LYZR_API_KEY}" ]] && ENV_ARGS+=("LYZR_API_KEY=${LYZR_API_KEY}")

if az containerapp show -n "${APP}" -g "${RG}" >/dev/null 2>&1; then
  echo "→ updating existing ${APP} → ${REF}"
  az containerapp update -n "${APP}" -g "${RG}" \
    --image "${REF}" \
    --set-env-vars "${ENV_ARGS[@]}" \
    --query "properties.provisioningState" -o tsv
else
  echo "→ creating ${APP} in ${ENVIRONMENT} → ${REF}"
  az containerapp create -n "${APP}" -g "${RG}" \
    --environment "${ENVIRONMENT}" \
    --image "${REF}" \
    --registry-server "${ACR}.azurecr.io" \
    --ingress external --target-port "${PORT}" \
    --min-replicas 1 --max-replicas 3 \
    --cpu 0.5 --memory 1Gi \
    --env-vars "${ENV_ARGS[@]}" \
    --query "properties.provisioningState" -o tsv
fi

FQDN="$(az containerapp show -n "${APP}" -g "${RG}" \
  --query "properties.configuration.ingress.fqdn" -o tsv)"
echo "✅ deployed — https://${FQDN}"
echo "   LiveKit-native: the browser joins Lyzr's hosted agent room directly;"
echo "   this app only proxies /sessions/start + /sessions/end (key stays server-side)."
