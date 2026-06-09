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
PROM_APP="${PROM_APP:-movate-dev-prometheus}"
IMAGE="${IMAGE:-mdk-grafana}"
# Pinned datasource uids the dashboards bind to (kept in sync with the
# provisioning YAML). The PromQL dashboards ship with ${DS_PROMETHEUS} /
# ${DS_INSIGHTS} placeholders (Grafana import-wizard inputs); we rewrite them to
# these literal uids during staging so file-provisioned dashboards bind with no
# manual datasource pick.
PROM_DS_UID="mdkpromds00001"
INSIGHTS_DS_UID="ffnrfwjnew5xcc"
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

# PromQL dashboards (ADR 087) → their own staged dir, with the import-wizard
# datasource placeholders rewritten to the pinned uids so file provisioning
# binds them. sed both the ${DS_PROMETHEUS} (→ Prometheus) and ${DS_INSIGHTS}
# (→ Azure Monitor) references.
mkdir -p "${STAGE}/dashboards-prom"
for f in "${ROOT}/dashboards/grafana/"mdk-*.json; do
  sed -e "s/\${DS_PROMETHEUS}/${PROM_DS_UID}/g" \
      -e "s/\${DS_INSIGHTS}/${INSIGHTS_DS_UID}/g" \
      "${f}" > "${STAGE}/dashboards-prom/$(basename "${f}")"
done
echo "→ staged $(ls "${STAGE}/dashboards" | wc -l | tr -d ' ') azure + $(ls "${STAGE}/dashboards-prom" | wc -l | tr -d ' ') promQL dashboard(s) + provisioning"

# Discover the internal Prometheus URL (same Container Apps environment) so the
# provisioned Prometheus datasource resolves. Empty when Prometheus isn't
# deployed (enablePrometheus=false) — the datasource then shows unconfigured and
# nothing else is affected.
PROM_FQDN="$(az containerapp show -n "${PROM_APP}" -g "${RG}" \
  --query "properties.configuration.ingress.fqdn" -o tsv 2>/dev/null || true)"
if [ -n "${PROM_FQDN}" ]; then
  PROM_URL="https://${PROM_FQDN}"
  echo "→ Prometheus datasource → ${PROM_URL}"
else
  PROM_URL=""
  echo "⚠ no ${PROM_APP} found — PromQL dashboards will provision but render empty until Prometheus is deployed (enablePrometheus=true)"
fi

echo "→ building ${REF}"
az acr build -r "${ACR}" -t "${IMAGE}:${TAG}" -f "${STAGE}/Dockerfile" "${STAGE}"

echo "→ rolling ${APP} to ${REF} (env GF_* preserved; PROMETHEUS_URL set)"
az containerapp update -n "${APP}" -g "${RG}" \
  --image "${REF}" \
  --set-env-vars "PROMETHEUS_URL=${PROM_URL}" \
  --query "properties.provisioningState" -o tsv

FQDN="$(az containerapp show -n "${APP}" -g "${RG}" \
  --query "properties.configuration.ingress.fqdn" -o tsv)"
echo "✅ deployed — https://${FQDN}/dashboards"
echo "   MDK folder (Azure Monitor): Temporal · Voice · Executive · Live Runtime"
echo "   MDK · Prometheus folder (PromQL): golden-signals · cost · queue-and-pool · runtime-overview · exec-summary · dead-letter"
