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
ROOT="$(cd "${HERE}/../../.." && pwd)"

# shellcheck source=/dev/null
source "${HERE}/urls.env"

# Control-plane login + deploy stamp. CP_TOKEN is the bearer key behind the
# login (inject from env: CP_TOKEN="$MDK_DEV_KEY"); DEPLOYED_AT is stamped now so
# the page can show "deployed <date> · <time> ago" (live in the browser).
CP_USER="${CP_USER:-movate}"
CP_PASS="${CP_PASS:-MovateDemo2026!}"
CP_TOKEN="${CP_TOKEN:-}"
DEPLOYED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ -z "$CP_TOKEN" ]]; then
  echo "⚠  CP_TOKEN is empty — the Agent Control Plane login will sign in but API calls will 401." >&2
  echo "   Re-run with: CP_TOKEN=\"\$MDK_DEV_KEY\" ./deploy-landing.sh" >&2
fi

# Pattern Catalog (#catalog in-page view) — statically generated from the mdk
# registry (movate.templates, the catalog behind `mdk patterns list`) so the
# page can never drift from what `mdk init --pattern` accepts. Needs the repo's
# uv env; fails the deploy loudly rather than shipping a page with a dead tile.
echo "→ rendering the pattern catalog from the registry (render_catalog.py)"
CATALOG_HTML="$(cd "${ROOT}" && uv run python "${HERE}/render_catalog.py")"
if [[ -z "$CATALOG_HTML" ]]; then
  echo "✗ render_catalog.py produced no output — pattern catalog would be empty" >&2
  exit 1
fi

html="$(cat "${HERE}/index.html.tmpl")"
html="${html//__PATTERN_CATALOG__/$CATALOG_HTML}"
html="${html//__OPENWEBUI_URL__/$OPENWEBUI_URL}"
html="${html//__PLAYGROUND_URL__/$PLAYGROUND_URL}"
html="${html//__GRAFANA_URL__/$GRAFANA_URL}"
html="${html//__TEMPORAL_UI_URL__/$TEMPORAL_UI_URL}"
html="${html//__LANGFUSE_URL__/$LANGFUSE_URL}"
html="${html//__API_URL__/$API_URL}"
html="${html//__CP_USER__/$CP_USER}"
html="${html//__CP_PASS__/$CP_PASS}"
html="${html//__CP_TOKEN__/$CP_TOKEN}"
html="${html//__DEPLOYED_AT__/$DEPLOYED_AT}"

if printf '%s' "$html" | grep -qE '__[A-Z_]+__'; then
  echo "✗ unresolved __PLACEHOLDER__ remains — check urls.env / deploy-landing.sh" >&2
  printf '%s' "$html" | grep -oE '__[A-Z_]+__' | sort -u >&2
  exit 1
fi

b64="$(printf '%s' "$html" | base64 | tr -d '\n')"
echo "→ updating ${APP} HTML_B64 ($(printf '%s' "$html" | grep -c '<a class="card"') tiles)"
az containerapp update -n "$APP" -g "$RG" --set-env-vars "HTML_B64=${b64}" \
  --query "properties.provisioningState" -o tsv
echo "✅ landing page deployed."
