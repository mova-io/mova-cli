#!/usr/bin/env bash
# Provision a self-hosted Temporal (server + Web UI) on a single Azure VM via
# docker-compose, backed by the EXISTING shared Azure Postgres. Reliable,
# direct-port networking (no ACA ingress) for the team's durable-workflow stack.
#
# Idempotent-ish: safe to re-run; `az vm create` errors if the VM exists (delete
# first to recreate). Reuses the shared Postgres `temporal`/`temporal_visibility`
# DBs — destroys NO data.
#
# Usage:
#   POSTGRES_PWD=... ./deploy-temporal-vm.sh
# or it will pull the password from Key Vault automatically.
set -euo pipefail

# ---- config (override via env) ---------------------------------------------
RG="${RG:-movate-dev-rg}"
LOCATION="${LOCATION:-eastus2}"
VM_NAME="${VM_NAME:-movate-dev-temporal-vm}"
VM_SIZE="${VM_SIZE:-Standard_B2s}"           # 2 vCPU / 4GiB — plenty for single-node Temporal
ADMIN_USER="${ADMIN_USER:-azureuser}"
KV_NAME="${KV_NAME:-movate-dev-kv-mvt}"
PG_PW_SECRET="${PG_PW_SECRET:-pg-admin-password}"
POSTGRES_SEEDS="${POSTGRES_SEEDS:-movate-dev-pg-mvt.postgres.database.azure.com}"
POSTGRES_USER="${POSTGRES_USER:-movateadmin}"
PG_SERVER_NAME="${PG_SERVER_NAME:-movate-dev-pg-mvt}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# ---- resolve the Postgres password (Key Vault unless already in env) --------
if [[ -z "${POSTGRES_PWD:-}" ]]; then
  echo "→ pulling Postgres password from Key Vault ${KV_NAME}/${PG_PW_SECRET}"
  POSTGRES_PWD="$(az keyvault secret show --vault-name "$KV_NAME" --name "$PG_PW_SECRET" --query value -o tsv)"
fi
[[ -n "$POSTGRES_PWD" ]] || { echo "✗ POSTGRES_PWD empty — set it or fix the KV secret name"; exit 1; }

# ---- build cloud-init (installs docker, writes compose + .env, starts it) ---
COMPOSE_B64="$(base64 < "${HERE}/docker-compose.yml" | tr -d '\n')"
CLOUD_INIT="$(mktemp)"
cat > "$CLOUD_INIT" <<EOF
#cloud-config
package_update: true
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - mkdir -p /opt/temporal
  - echo "${COMPOSE_B64}" | base64 -d > /opt/temporal/docker-compose.yml
  - |
    cat > /opt/temporal/.env <<ENVEOF
    POSTGRES_SEEDS=${POSTGRES_SEEDS}
    POSTGRES_USER=${POSTGRES_USER}
    POSTGRES_PWD=${POSTGRES_PWD}
    ENVEOF
  - chmod 600 /opt/temporal/.env
  - cd /opt/temporal && docker compose up -d
EOF

# ---- create the VM ---------------------------------------------------------
echo "→ creating VM ${VM_NAME} (${VM_SIZE}) in ${RG}"
az vm create \
  --resource-group "$RG" --name "$VM_NAME" --location "$LOCATION" \
  --image Ubuntu2204 --size "$VM_SIZE" \
  --admin-username "$ADMIN_USER" --generate-ssh-keys \
  --public-ip-sku Standard \
  --custom-data "$CLOUD_INIT" \
  --output table
rm -f "$CLOUD_INIT"

# ---- open ports: 22 (ssh), 7233 (gRPC), 8080 (UI) --------------------------
echo "→ opening NSG ports 22 / 7233 / 8080"
az vm open-port --resource-group "$RG" --name "$VM_NAME" --port 7233 --priority 1010 >/dev/null
az vm open-port --resource-group "$RG" --name "$VM_NAME" --port 8080 --priority 1020 >/dev/null

# ---- stable DNS label (#767): a VM rebuild changes the public IP, which would
# silently break the worker's TEMPORAL_HOST + the landing tile. A DNS label gives
# a stable <label>.<region>.cloudapp.azure.com name that survives IP changes.
DNS_LABEL="${DNS_LABEL:-$VM_NAME}"
PIP_NAME="$(az vm show -g "$RG" -n "$VM_NAME" --query "networkProfile.networkInterfaces[0].id" -o tsv \
  | xargs -I{} az network nic show --ids {} --query "ipConfigurations[0].publicIPAddress.id" -o tsv \
  | xargs -I{} basename {})"
VM_FQDN="$(az network public-ip update -g "$RG" -n "$PIP_NAME" --dns-name "$DNS_LABEL" \
  --query "dnsSettings.fqdn" -o tsv)"
echo "→ DNS label set: ${VM_FQDN}"

# ---- allow the VM's public IP through the Postgres firewall -----------------
VM_IP="$(az vm list-ip-addresses -g "$RG" -n "$VM_NAME" --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv)"
echo "→ allowing VM IP ${VM_IP} through Postgres firewall"
az postgres flexible-server firewall-rule create \
  --resource-group "$RG" --name "$PG_SERVER_NAME" \
  --rule-name "temporal-vm" --start-ip-address "$VM_IP" --end-ip-address "$VM_IP" \
  --output none

# Prefer the stable DNS FQDN over the bare IP for all wiring.
ADDR="${VM_FQDN:-$VM_IP}"
cat <<DONE

✅ Temporal VM provisioned. Docker pulls + first-boot schema check take ~2-3 min.

   Web UI (team):   http://${ADDR}:8080
   Worker gRPC:     ${ADDR}:7233

Point the mdk worker at it:
   TEMPORAL_HOST=${ADDR}:7233 TEMPORAL_NAMESPACE=default mdk worker --backend temporal ...

Update the deployed apps (worker / temporal-ui tile) to TEMPORAL_HOST=${ADDR}:7233.
DONE
