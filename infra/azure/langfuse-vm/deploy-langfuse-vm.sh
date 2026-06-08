#!/usr/bin/env bash
# Provision self-hosted Langfuse v3 (web + worker + ClickHouse + Redis + MinIO)
# on a single Azure VM via docker-compose, reusing the shared Azure Postgres for
# the transactional `langfuse` database. Replaces the ACA langfuse v2 app (which
# silently dropped v3-SDK traces).
#
# Two-phase so secrets never land in VM custom-data: cloud-init installs Docker +
# the compose file (no secrets); then we SSH the .env (secrets from Key Vault) and
# bring the stack up.
#
# Usage:  ./deploy-langfuse-vm.sh
set -euo pipefail

RG="${RG:-movate-dev-rg}"
LOCATION="${LOCATION:-eastus2}"
VM_NAME="${VM_NAME:-movate-dev-langfuse-vm}"
VM_SIZE="${VM_SIZE:-Standard_B2ms}"          # 2 vCPU / 8 GiB — ClickHouse needs headroom
ADMIN_USER="${ADMIN_USER:-azureuser}"
KV_NAME="${KV_NAME:-movate-dev-kv-mvt}"
PG_SERVER_NAME="${PG_SERVER_NAME:-movate-dev-pg-mvt}"
PG_FQDN="${PG_FQDN:-movate-dev-pg-mvt.postgres.database.azure.com}"
PG_USER="${PG_USER:-movateadmin}"
HERE="$(cd "$(dirname "$0")" && pwd)"

kv() { az keyvault secret show --vault-name "$KV_NAME" --name "$1" --query value -o tsv; }

echo "→ pulling Langfuse secrets from Key Vault"
PG_PW="$(kv pg-admin-password)"
LF_SALT="$(kv langfuse-salt)"
LF_ENC="$(kv langfuse-encryption-key)"
LF_NEXTAUTH="$(kv langfuse-nextauth-secret)"
LF_PUB="$(kv langfuse-public-key)"
LF_SEC="$(kv langfuse-secret-key)"
INIT_PW="${INIT_PW:-MovateDemo2026!}"
# Compose-local datastore creds (generated; not externally reachable).
CH_PW="$(openssl rand -hex 16)"; REDIS_PW="$(openssl rand -hex 16)"; MINIO_PW="$(openssl rand -hex 16)"
# Langfuse v3 needs the password URL-encoded in DATABASE_URL.
ENC_PG="$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "$PG_PW")"
LF_DB_URL="postgresql://${PG_USER}:${ENC_PG}@${PG_FQDN}:5432/langfuse?sslmode=require"

# ---- cloud-init: docker + compose file only (no secrets) -------------------
COMPOSE_B64="$(base64 < "${HERE}/docker-compose.yml" | tr -d '\n')"
CI="$(mktemp)"
cat > "$CI" <<EOF
#cloud-config
package_update: true
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - mkdir -p /opt/langfuse
  - echo "${COMPOSE_B64}" | base64 -d > /opt/langfuse/docker-compose.yml
EOF

echo "→ creating VM ${VM_NAME} (${VM_SIZE})"
az vm create -g "$RG" -n "$VM_NAME" --location "$LOCATION" \
  --image Ubuntu2204 --size "$VM_SIZE" --admin-username "$ADMIN_USER" \
  --generate-ssh-keys --public-ip-sku Standard --custom-data "$CI" -o table
rm -f "$CI"
az vm open-port -g "$RG" -n "$VM_NAME" --port 3000 --priority 1010 >/dev/null

VM_IP="$(az vm list-ip-addresses -g "$RG" -n "$VM_NAME" --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv)"
echo "→ allowing VM IP ${VM_IP} through Postgres firewall"
az postgres flexible-server firewall-rule create -g "$RG" --name "$PG_SERVER_NAME" \
  --rule-name "langfuse-vm" --start-ip-address "$VM_IP" --end-ip-address "$VM_IP" -o none

SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 ${ADMIN_USER}@${VM_IP}"
echo "→ waiting for cloud-init (docker install)…"
for _ in $(seq 1 30); do $SSH 'command -v docker >/dev/null 2>&1' && break; sleep 10; done

echo "→ resetting the langfuse Postgres schema (clean v3 migration; demo data is throwaway)"
$SSH "sudo docker run --rm -e PGPASSWORD='${PG_PW}' postgres:16 psql \
  'host=${PG_FQDN} dbname=langfuse user=${PG_USER} sslmode=require' \
  -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO ${PG_USER}; GRANT ALL ON SCHEMA public TO PUBLIC;'"

echo "→ writing .env + bringing the stack up"
$SSH "sudo bash -c 'cat > /opt/langfuse/.env' <<ENVEOF
LANGFUSE_DATABASE_URL=${LF_DB_URL}
LANGFUSE_SALT=${LF_SALT}
LANGFUSE_ENCRYPTION_KEY=${LF_ENC}
LANGFUSE_NEXTAUTH_SECRET=${LF_NEXTAUTH}
LANGFUSE_NEXTAUTH_URL=http://${VM_IP}:3000
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=${LF_PUB}
LANGFUSE_INIT_PROJECT_SECRET_KEY=${LF_SEC}
LANGFUSE_INIT_USER_PASSWORD=${INIT_PW}
CLICKHOUSE_PASSWORD=${CH_PW}
REDIS_PASSWORD=${REDIS_PW}
MINIO_PASSWORD=${MINIO_PW}
ENVEOF
sudo chmod 600 /opt/langfuse/.env"
$SSH "cd /opt/langfuse && sudo docker compose up -d"

cat <<DONE

✅ Langfuse v3 provisioning started (ClickHouse migration + first boot ~3-5 min).
   Web UI:   http://${VM_IP}:3000   (login: demo@movate.dev / ${INIT_PW})
   Project:  MDK (keys = the LANGFUSE_PUBLIC/SECRET_KEY the apps already send)

Point the apps at it:  LANGFUSE_HOST=http://${VM_IP}:3000  on api/worker/temporal-worker.
DONE
