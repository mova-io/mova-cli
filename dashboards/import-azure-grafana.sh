#!/usr/bin/env bash
# Import the Azure-Monitor Grafana dashboards (dashboards/grafana/azure/*.json)
# into a running Grafana via its HTTP API. The bridge until file-based
# provisioning lands (#764): the source dashboards are version-controlled here,
# but a fresh Grafana only shows what's been imported — this makes them all live
# in one command.
#
# The dashboards target the Azure Monitor datasource (uid ffnrfwjnew5xcc); that
# datasource must already exist in the target Grafana (it does on movate-dev).
#
# Usage:
#   GRAFANA_URL=https://…grafana-oss… GRAFANA_PASSWORD=… dashboards/import-azure-grafana.sh
#   # GRAFANA_USER defaults to "admin". Reads creds from env only (never args,
#   # so they stay out of shell history / process list).
#
# Tip for the movate-dev demo (pull the admin password from the container app):
#   GRAFANA_PASSWORD="$(az containerapp show -n movate-dev-grafana-oss -g movate-dev-rg \
#     --query "properties.template.containers[0].env[?name=='GF_SECURITY_ADMIN_PASSWORD'].value | [0]" -o tsv)" \
#   GRAFANA_URL=https://movate-dev-grafana-oss.bluebush-9aec1e70.eastus2.azurecontainerapps.io \
#     dashboards/import-azure-grafana.sh
set -euo pipefail

URL="${GRAFANA_URL:?set GRAFANA_URL to the Grafana base URL}"
USER="${GRAFANA_USER:-admin}"
PASS="${GRAFANA_PASSWORD:?set GRAFANA_PASSWORD (Grafana admin password)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
URL="${URL%/}"

shopt -s nullglob
files=("${HERE}/grafana/azure"/*.json)
[ ${#files[@]} -gt 0 ] || { echo "no dashboards under grafana/azure/" >&2; exit 1; }

ok=0; fail=0
for f in "${files[@]}"; do
  title="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('title','?'))" "$f")"
  # Wrap the bare dashboard JSON in the import envelope; null the id so it's a
  # create-or-overwrite by uid, and overwrite=true so re-running is idempotent.
  body="$(python3 - "$f" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["id"] = None
print(json.dumps({"dashboard": d, "overwrite": True, "folderId": 0, "message": "imported via dashboards/import-azure-grafana.sh"}))
PY
)"
  code="$(curl -s -o /tmp/gimport.out -w "%{http_code}" -X POST "${URL}/api/dashboards/db" \
    -u "${USER}:${PASS}" -H "Content-Type: application/json" --data "${body}")"
  if [ "$code" = "200" ]; then
    echo "✅ $(basename "$f")  —  ${title}"
    ok=$((ok+1))
  else
    echo "✗ $(basename "$f")  —  HTTP ${code}: $(head -c 200 /tmp/gimport.out)" >&2
    fail=$((fail+1))
  fi
done
echo "done: ${ok} imported, ${fail} failed → ${URL}/dashboards"
[ "$fail" -eq 0 ]
