#!/usr/bin/env bash
# Build + deploy the movate-dev Grafana with provisioned dashboards (#764).
# Builds a custom grafana-oss image that bakes in the Azure Monitor datasource +
# the in-repo azure/ dashboards (file provisioning), then rolls the
# movate-dev-grafana-oss Container App to it. After this, a fresh Grafana shows
# Temporal / Voice / Executive / Live Runtime under the "MDK" folder with no
# manual import — and they survive future redeploys (grafana-oss has no volume).
#
# Builds from a STAGED context (the provisioning configs live under infra/, which
# the repo .dockerignore excludes — staging avoids touching that shared ignore).
#
# Usage:  infra/azure/grafana/deploy.sh
set -euo pipefail

ACR="${ACR:-movatedevacrmvt}"
RG="${RG:-movate-dev-rg}"
APP="${APP:-movate-dev-grafana-oss}"
IMAGE="${IMAGE:-mdk-grafana}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
SHA="$(git -C "${ROOT}" rev-parse --short HEAD)"
TAG="${TAG:-prov-${SHA}}"
REF="${ACR}.azurecr.io/${IMAGE}:${TAG}"

# Assemble a minimal build context: Dockerfile + provisioning + the azure dashboards.
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
cp "${HERE}/Dockerfile" "${STAGE}/Dockerfile"
cp -R "${HERE}/provisioning" "${STAGE}/provisioning"
mkdir -p "${STAGE}/dashboards"
cp "${ROOT}/dashboards/grafana/azure/"*.json "${STAGE}/dashboards/"
echo "→ staged $(ls "${STAGE}/dashboards" | wc -l | tr -d ' ') dashboard(s) + provisioning"

echo "→ building ${REF}"
az acr build -r "${ACR}" -t "${IMAGE}:${TAG}" -f "${STAGE}/Dockerfile" "${STAGE}"

echo "→ rolling ${APP} to ${REF} (env GF_* preserved by the update)"
az containerapp update -n "${APP}" -g "${RG}" \
  --image "${REF}" \
  --query "properties.provisioningState" -o tsv

FQDN="$(az containerapp show -n "${APP}" -g "${RG}" \
  --query "properties.configuration.ingress.fqdn" -o tsv)"
echo "✅ deployed — https://${FQDN}/dashboards (MDK folder: Temporal · Voice · Executive · Live Runtime)"
