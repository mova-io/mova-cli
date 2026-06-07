#!/usr/bin/env bash
# Pre-flight / doctor for the self-hosted Temporal deployment on Azure (ADR 078/080).
# Read-only. Checks the prerequisites + live health that bit us one-at-a-time
# during the first deploy, so you get a single green/red instead of grepping
# 500-line Go stack traces. Run it before a deploy (prereqs) or after (health).
#
#   ./scripts/temporal-preflight.sh
#   RG=movate-prod-rg PG=movate-prod-pg ... ./scripts/temporal-preflight.sh
#
# Exit code: non-zero if any hard check FAILS (prerequisites that block a healthy
# deploy). WARN/INFO never fail the run.
set -uo pipefail

RG="${RG:-movate-dev-rg}"
PG="${PG:-movate-dev-pg-mvt}"
KV="${KV:-movate-dev-kv-mvt}"
TEMPORAL_APP="${TEMPORAL_APP:-movate-dev-temporal}"
WORKER_APP="${WORKER_APP:-${TEMPORAL_APP}-worker}"
PG_SECRET="${PG_SECRET:-pg-admin-password}"
MIN_CONNS="${MIN_CONNS:-100}"   # Temporal's startup burst (~42) + stack + headroom

fails=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; fails=$((fails+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

command -v az >/dev/null || { echo "az CLI not found" >&2; exit 1; }
az account show >/dev/null 2>&1 || { echo "not logged in — run 'az login'" >&2; exit 1; }

hdr "Key Vault"
if az keyvault secret show --vault-name "$KV" --name "$PG_SECRET" --query id -o tsv >/dev/null 2>&1; then
  pass "secret '$PG_SECRET' present in $KV (apps + temporal read it for PG auth)"
else
  fail "secret '$PG_SECRET' missing in $KV — PG auth will fail. Set it (and ensure it matches the live PG password)."
fi

hdr "Postgres ($PG)"
MAXC=$(az postgres flexible-server parameter show -g "$RG" -s "$PG" --name max_connections --query value -o tsv 2>/dev/null || echo "?")
if [ "$MAXC" = "?" ]; then
  fail "could not read max_connections (server/RG wrong, or not provisioned)"
elif [ "$MAXC" -ge "$MIN_CONNS" ] 2>/dev/null; then
  pass "max_connections=$MAXC (>= $MIN_CONNS — room for Temporal's pools + the app stack)"
else
  fail "max_connections=$MAXC (< $MIN_CONNS) → Temporal's startup burst hits 'reserved for SUPERUSER'. Raise it + restart PG."
fi

EXT=$(az postgres flexible-server parameter show -g "$RG" -s "$PG" --name azure.extensions --query value -o tsv 2>/dev/null || echo "")
for want in BTREE_GIN PG_TRGM; do
  if printf '%s' "$EXT" | grep -qi "$want"; then
    pass "azure.extensions allow-lists $want (Temporal visibility schema)"
  else
    fail "azure.extensions missing $want → 'CREATE EXTENSION not allow-listed' during schema setup. Current: '${EXT:-<empty>}'"
  fi
done

RST=$(az postgres flexible-server parameter show -g "$RG" -s "$PG" --name require_secure_transport --query value -o tsv 2>/dev/null || echo "?")
if [ "$RST" = "on" ]; then
  pass "require_secure_transport=on (server must connect via SQL_TLS_* — set in containerapp-temporal.bicep)"
else
  warn "require_secure_transport=$RST (SSL not mandated; fine, but prefer 'on' with SQL_TLS_* configured)"
fi

SUBID=$(az account show --query id -o tsv 2>/dev/null)
PEAK=$(az monitor metrics list --resource "/subscriptions/$SUBID/resourceGroups/$RG/providers/Microsoft.DBforPostgreSQL/flexibleServers/$PG" \
  --metric active_connections --interval PT1M --aggregation Maximum -o json 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);v=[p.get('maximum') for p in d['value'][0]['timeseries'][0]['data'] if p.get('maximum') is not None];print(int(max(v)) if v else -1)" 2>/dev/null || echo "-1")
[ "${PEAK:-"-1"}" != "-1" ] && pass "recent peak active_connections=$PEAK of $MAXC"

hdr "Container Apps"
for app in "$TEMPORAL_APP" "$WORKER_APP"; do
  RUN=$(az containerapp show -n "$app" -g "$RG" --query "properties.runningStatus" -o tsv 2>/dev/null || echo "MISSING")
  case "$RUN" in
    Running|RunningAtMaxScale) pass "$app: $RUN" ;;
    MISSING) warn "$app: not deployed yet (deploy with enableTemporal=true)" ;;
    *) fail "$app: runningStatus=$RUN (check logs: az containerapp logs show -n $app -g $RG --tail 40)" ;;
  esac
done

hdr "Result"
if [ "$fails" -eq 0 ]; then
  printf '\033[32mAll hard checks passed.\033[0m\n'
  exit 0
else
  printf '\033[31m%d check(s) failed — fix before/after deploy (see hints above).\033[0m\n' "$fails"
  exit 1
fi
