#!/usr/bin/env bash
# Build + deploy the Mova-iO Voice Playground — the generic standalone Lyzr voice
# adapter (plug in ANY Lyzr/Mova-iO agent via the BYOK panel) — to its own Azure
# Container App, separate from the POS-branded mdk-pos-demo. Creates the app on
# first run, updates it thereafter. Build context is the REPO ROOT so the
# package's movate.voice source installs.
#
# Usage:
#   mova-voice-playground/deploy.sh                  # build a git-sha tag + create/roll the app
#   TAG=mytag mova-voice-playground/deploy.sh        # custom image tag
#   SKIP_BUILD=1 TAG=<existing> mova-voice-playground/deploy.sh   # redeploy an existing image
set -euo pipefail

ACR="${ACR:-movatedevacrmvt}"
RG="${RG:-movate-dev-rg}"
ENVIRONMENT="${ENVIRONMENT:-movate-dev-cae}"
APP="${APP:-mova-voice-playground}"
IMAGE="${IMAGE:-mova-voice-playground}"
PORT="${PORT:-8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

SHA="$(git -C "${ROOT}" rev-parse --short HEAD)"
TAG="${TAG:-vp-${SHA}}"
REF="${ACR}.azurecr.io/${IMAGE}:${TAG}"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  # ADR 066 — pass the git-derived CalVer as a build-arg (the build context
  # excludes .git, so the hatch metadata hook can't derive it in-image).
  VERSION="$(python3 "${ROOT}/scripts/calver_version.py" 2>/dev/null || echo '0+unknown')"
  echo "→ building ${REF} (context: repo root, CalVer ${VERSION})"
  az acr build -r "${ACR}" -t "${IMAGE}:${TAG}" \
    --build-arg "MOVATE_BUILD_VERSION=${VERSION}" \
    -f "${HERE}/Dockerfile" "${ROOT}"
fi

# STT/TTS + the default server-side agent use the operator's OPENAI_API_KEY;
# users plug in their own Lyzr/Mova-iO agent via the in-UI BYOK panel (per-session,
# never persisted). Set LYZR_API_KEY + LYZR_AGENT_ID here only if you want a
# server-side default Lyzr agent instead of OpenAI.
OPENAI_KEY="${OPENAI_API_KEY:-$(az containerapp show -n mdk-pos-demo -g "${RG}" \
  --query "properties.template.containers[0].env[?name=='OPENAI_API_KEY'].value | [0]" -o tsv 2>/dev/null || true)}"

if az containerapp show -n "${APP}" -g "${RG}" >/dev/null 2>&1; then
  echo "→ updating existing ${APP} → ${REF}"
  az containerapp update -n "${APP}" -g "${RG}" \
    --image "${REF}" \
    --set-env-vars "PORT=${PORT}" "BUILD_TAG=${TAG}" \
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
    --env-vars "PORT=${PORT}" "BUILD_TAG=${TAG}" ${OPENAI_KEY:+"OPENAI_API_KEY=${OPENAI_KEY}"} \
    --query "properties.provisioningState" -o tsv
fi

FQDN="$(az containerapp show -n "${APP}" -g "${RG}" \
  --query "properties.configuration.ingress.fqdn" -o tsv)"
echo "✅ deployed — https://${FQDN}"
echo "   Generic Lyzr voice adapter: paste a Mova-iO (Lyzr) key + agent in the BYOK"
echo "   panel to voice ANY hosted agent through the STT/TTS pipeline."
