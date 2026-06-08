#!/usr/bin/env bash
# Render + deploy the movate-dev landing page (the tile index at the public
# landing URL). Source-controlled fix for #761: the page used to live ONLY as the
# container's HTML_B64 env (created out-of-band), so every tile — and edits like
# the Temporal UI / Langfuse tiles + shared login — vanished on a full redeploy.
#
# This renders infra/azure/landing/index.html.tmpl with the tile URLs from
# urls.env (override via env) and updates the landing Container App's HTML_B64.
#
# Usage:  ./deploy-landing.sh            # uses urls.env defaults
#         LANGFUSE_URL=http://... ./deploy-landing.sh   # override a tile URL
set -euo pipefail

RG="${RG:-movate-dev-rg}"
APP="${APP:-movate-dev-landing}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=/dev/null
source "${HERE}/urls.env"

html="$(cat "${HERE}/index.html.tmpl")"
html="${html//__OPENWEBUI_URL__/$OPENWEBUI_URL}"
html="${html//__PLAYGROUND_URL__/$PLAYGROUND_URL}"
html="${html//__GRAFANA_URL__/$GRAFANA_URL}"
html="${html//__TEMPORAL_UI_URL__/$TEMPORAL_UI_URL}"
html="${html//__LANGFUSE_URL__/$LANGFUSE_URL}"
html="${html//__API_URL__/$API_URL}"

if [[ "$html" == *"__"*"_URL__"* ]]; then
  echo "✗ unresolved placeholder remains — check urls.env" >&2; exit 1
fi

b64="$(printf '%s' "$html" | base64 | tr -d '\n')"
echo "→ updating ${APP} HTML_B64 ($(printf '%s' "$html" | grep -c '<a class="card"') tiles)"
az containerapp update -n "$APP" -g "$RG" --set-env-vars "HTML_B64=${b64}" \
  --query "properties.provisioningState" -o tsv
echo "✅ landing page deployed."
