#!/usr/bin/env bash
#
# import-movate-dashboards.sh — ADR 039 Phase 1
#
# Bulk-imports the illustrative fleet dashboards from
# `dashboards/grafana/movate/*.json` into Movate's Azure Managed Grafana
# instance (the one provisioned by infra/movate-telemetry/managed-grafana.bicep).
# Each dashboard is upserted via `az grafana dashboard create --overwrite`,
# keyed by the dashboard's `uid` field, so reruns are idempotent and safe.
#
# Usage:
#   bash scripts/import-movate-dashboards.sh \
#     --grafana-name movate-fleet-grafana \
#     --resource-group movate-telemetry-rg
#
#   # or via env
#   MOVATE_GRAFANA_NAME=movate-fleet-grafana \
#   MOVATE_GRAFANA_RG=movate-telemetry-rg \
#     bash scripts/import-movate-dashboards.sh
#
# Pre-reqs:
#   - az CLI logged in to Movate's tenant.
#   - The `amg` extension installed: `az extension add --name amg`.
#   - jq on PATH (used to read each dashboard's uid).
#
# Dependency: the dashboard JSONs live on the ADR 039 PR (#523) branch
# (origin/docs/adr-039-movate-product-telemetry). After that PR merges to
# main, this script finds them under dashboards/grafana/movate/ and runs.
# Until then, this script exits with a clear message.

set -euo pipefail

# --- Arg parsing ------------------------------------------------------------

GRAFANA_NAME="${MOVATE_GRAFANA_NAME:-}"
RESOURCE_GROUP="${MOVATE_GRAFANA_RG:-}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --grafana-name)
            GRAFANA_NAME="$2"
            shift 2
            ;;
        --resource-group)
            RESOURCE_GROUP="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '3,30p' "$0"
            exit 0
            ;;
        *)
            printf 'unknown arg: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

if [ -z "$GRAFANA_NAME" ]; then
    printf 'error: --grafana-name (or MOVATE_GRAFANA_NAME) is required\n' >&2
    exit 2
fi
if [ -z "$RESOURCE_GROUP" ]; then
    printf 'error: --resource-group (or MOVATE_GRAFANA_RG) is required\n' >&2
    exit 2
fi

# --- Dependency tools -------------------------------------------------------

if ! command -v az >/dev/null 2>&1; then
    printf 'error: az CLI not on PATH\n' >&2
    exit 3
fi
if ! command -v jq >/dev/null 2>&1; then
    printf 'error: jq not on PATH (needed to read dashboard uids)\n' >&2
    exit 3
fi

# --- Dashboard source dir ---------------------------------------------------

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DASHBOARDS_DIR="$REPO_ROOT/dashboards/grafana/movate"

if [ ! -d "$DASHBOARDS_DIR" ]; then
    printf 'Dependency: dashboards live on the ADR-039 PR #523 branch -- merge that first, then re-run from main.\n' >&2
    exit 4
fi

# Glob may be empty even if dir exists — guard.
shopt -s nullglob
dashboards=("$DASHBOARDS_DIR"/*.json)
shopt -u nullglob

if [ "${#dashboards[@]}" -eq 0 ]; then
    printf 'Dependency: %s exists but contains no *.json dashboards. Merge PR #523 to populate it.\n' "$DASHBOARDS_DIR" >&2
    exit 4
fi

# --- Per-dashboard upsert ---------------------------------------------------

ok=0
skip=0
fail=0

for dash in "${dashboards[@]}"; do
    name="$(basename "$dash")"
    uid="$(jq -r '.uid // empty' "$dash")"
    if [ -z "$uid" ]; then
        printf '[skipped] %s -- no uid in JSON, refusing to import (idempotent upsert needs a stable uid)\n' "$name"
        skip=$((skip + 1))
        continue
    fi

    # `az grafana dashboard create --overwrite` upserts by uid. Output goes
    # to /dev/null on success; capture stderr for the failure line.
    if err="$(az grafana dashboard create \
                --grafana-name "$GRAFANA_NAME" \
                --resource-group "$RESOURCE_GROUP" \
                --definition "@$dash" \
                --overwrite \
                --output none 2>&1)"; then
        printf '[imported] %s (uid=%s)\n' "$name" "$uid"
        ok=$((ok + 1))
    else
        printf '[failed] %s (uid=%s): %s\n' "$name" "$uid" "${err//$'\n'/ }" >&2
        fail=$((fail + 1))
    fi
done

printf '\nsummary: %d imported, %d skipped, %d failed\n' "$ok" "$skip" "$fail"

# Non-zero exit if anything failed, so CI / runbook callers notice.
if [ "$fail" -gt 0 ]; then
    exit 1
fi
