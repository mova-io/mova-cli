#!/usr/bin/env bash
# Bind a custom domain (e.g. voice.movate.io) to the deployed mdk-voice
# Container App, with an Azure-managed TLS certificate.
#
# Flow (idempotent — re-run to refresh the cert or after fixing DNS):
#   1. Show the DNS records you need to add to your zone (CNAME + TXT).
#   2. Wait for those records to propagate (you can run this anytime; if DNS
#      isn't ready the bind step fails with a clear "DNS verification failed"
#      and you re-run).
#   3. Register the hostname on the app and provision the managed certificate.
#   4. Print the final URL.
#
# Usage:
#   ./scripts/setup_custom_domain.sh voice.movate.io
#   APP=my-demo RG=my-rg ./scripts/setup_custom_domain.sh voice.example.com
#
# Requires:
#   - az CLI logged in to the right subscription
#   - The mdk-voice Container App already deployed (run deploy_azure.sh first)
#   - Ownership of the DNS zone so you can add CNAME + TXT records

set -euo pipefail

DOMAIN="${1:-}"
[[ -z "$DOMAIN" ]] && { echo "usage: $0 <fqdn>   e.g. $0 voice.movate.io" >&2; exit 1; }

# Match deploy_azure.sh defaults (overridable via env, same as the deploy script).
SUB="${SUB:-8fab0f8f-b577-45d7-a485-ec32f73b22be}"
RG="${RG:-rg-mdk-voice-demo}"
APP="${APP:-mdk-voice-demo}"
ENV_NAME="${ENV_NAME:-cae-mdk-voice-demo}"

B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; C=$'\e[36m'; N=$'\e[0m'
step() { printf "\n${B}▸ %s${N}\n" "$*"; }
ok()   { printf "  ${G}✓${N} %s\n" "$*"; }
warn() { printf "  ${Y}⚠${N} %s\n" "$*"; }

az account set --subscription "$SUB" >/dev/null

step "Looking up the Container Apps environment + app FQDN"
APP_FQDN="$(az containerapp show -n "$APP" -g "$RG" --query "properties.configuration.ingress.fqdn" -o tsv)"
ASUID="$(az containerapp env show -n "$ENV_NAME" -g "$RG" \
    --query "properties.customDomainConfiguration.customDomainVerificationId" -o tsv 2>/dev/null || echo "")"
if [[ -z "$ASUID" ]]; then
    # On some ACA env shapes the property lives on the app itself.
    ASUID="$(az containerapp show -n "$APP" -g "$RG" \
        --query "properties.customDomainVerificationId" -o tsv)"
fi
ok "app FQDN:  $APP_FQDN"
ok "asuid:     $ASUID"

step "DNS records you need to add to the zone for ${C}${DOMAIN}${N}"
cat <<EOF
  ${C}CNAME${N}   ${DOMAIN}                  →   ${APP_FQDN}
  ${C}TXT${N}     asuid.${DOMAIN}            →   ${ASUID}

  • CNAME proves the hostname routes to the app.
  • TXT (record name MUST start with 'asuid.') is the ownership proof Azure
    requires before it'll issue a managed cert.

  Verify with:
    dig +short ${DOMAIN}
    dig +short TXT asuid.${DOMAIN}

  Both should return the values above. CNAME usually propagates in <5 min;
  TXT can take up to an hour depending on your zone's TTL.
EOF

read -r -p "Press ENTER once DNS is in place, or Ctrl-C to come back later: " _

step "Registering hostname + provisioning managed TLS cert"
# This is the modern one-shot: adds the hostname, runs CNAME-based DNS
# validation, requests the managed cert, binds it. Idempotent — safe to re-run.
az containerapp hostname add \
    --name "$APP" \
    --resource-group "$RG" \
    --hostname "$DOMAIN" \
    --validation-method CNAME \
    -o table || warn "hostname add may already exist — continuing"

az containerapp hostname bind \
    --name "$APP" \
    --resource-group "$RG" \
    --hostname "$DOMAIN" \
    --environment "$ENV_NAME" \
    --validation-method CNAME \
    -o table

step "Done"
printf "\n${G}${B}▸ Custom-domain URL${N}\n  https://${DOMAIN}/\n\n"
printf "${B}▸ Twilio voice URL (point your Twilio number here):${N}\n"
printf "  https://${DOMAIN}/twiml/voice\n"
printf "  wss://${DOMAIN}/ws/twilio\n\n"
printf "${B}▸ Notes${N}\n"
printf "  • Cert auto-renews every 6 months — no action needed.\n"
printf "  • To remove: az containerapp hostname delete -n ${APP} -g ${RG} --hostname ${DOMAIN}\n"
printf "  • The old delightfulcoast-... URL still works (alias, not replacement).\n"
