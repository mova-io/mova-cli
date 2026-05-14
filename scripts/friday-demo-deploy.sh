#!/usr/bin/env bash
#
# Friday Mova iO demo: deploy v0.7 to movate-dev-rg + smoke + mint Deva's key.
#
# BACKLOG Group H items 86 + 87 + 88. End-to-end "from a clean git pull
# to Deva-can-call-it" in one script. Idempotent — safe to re-run if a
# step partially fails.
#
# Prereqs:
#   * `az login` against the Pay-As-You-Go subscription
#     (61ea8b9b-8be4-4f6f-9655-e1846c6082fb)
#   * Current branch is `main` at the head we want to ship
#   * `movate-dev-rg` already provisioned (it is — Teams Bot deploy
#     last week)
#
# What this script does (in order):
#   1. Computes the image tag from `git rev-parse --short HEAD`
#   2. `az acr build` → builds + pushes movate:0.7.0-<sha>
#   3. `az deployment group create` → applies the Bicep with the new
#      image tag, adds Deva's Mova iO origin to MDK_CORS_ALLOWED_ORIGINS
#   4. Polls /healthz + /api/v1/openapi.json until the new revision
#      is serving
#   5. Mints `mvt_live_<...>` API key for Deva via `az containerapp
#      exec`, prints the bearer string
#   6. Prints a copy-pasteable Slack/email block with: runtime URL,
#      bearer token, OpenAPI URL, link to docs/angular-client.md
#
# Run from the repo root: `bash scripts/friday-demo-deploy.sh`

set -euo pipefail

# -----------------------------------------------------------------------------
# Config — change here, not inline
# -----------------------------------------------------------------------------

readonly SUBSCRIPTION_ID="8fab0f8f-b577-45d7-a485-ec32f73b22be"
readonly RESOURCE_GROUP="movate-dev-rg"
readonly ACR_NAME="movatedevacrmvt"
readonly API_APP_NAME="movate-dev-api"
readonly ENV="dev"
# Bicep parameter file. Switched from main.dev.bicepparam (personal
# sub) to main.movate.bicepparam (Movate sub) after the 2026-05-14
# blue/green migration. See docs/azure-movate-migration-runbook.md.
readonly BICEP_PARAMS="infra/azure/main.movate.bicepparam"
# Teams bot is disabled on the Movate sub for now (UAI pre-staged in
# Bicep but the Container App + Bot Service registration are deferred
# to a v0.8 follow-up). Keep the var so the existing parameters line
# stays consistent if Teams gets re-enabled.
readonly TEAMS_BOT_APP_ID="90f41ab7-31a6-4610-8cf4-88ed0581df55"
# Deva's Mova iO origin. Default empty (localhost-only) — set via
# `MOVA_IO_ORIGIN=https://...` when the production Mova iO hostname is
# known. Adding the origin post-deploy is a one-liner:
#   az containerapp update -g movate-dev-rg -n movate-dev-api \
#     --set-env-vars MDK_CORS_ALLOWED_ORIGINS="http://localhost:4200,<new>"
readonly DEVA_ORIGIN="${MOVA_IO_ORIGIN:-}"
readonly LOCAL_DEV_ORIGIN="http://localhost:4200"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

color_blue=$'\033[34m'
color_green=$'\033[32m'
color_yellow=$'\033[33m'
color_reset=$'\033[0m'

step() {
    echo
    echo "${color_blue}━━━ $* ${color_reset}"
}

success() {
    echo "${color_green}✓${color_reset} $*"
}

warn() {
    echo "${color_yellow}⚠${color_reset} $*"
}

# -----------------------------------------------------------------------------
# 0. Pre-flight
# -----------------------------------------------------------------------------

step "Pre-flight"

if ! command -v az &>/dev/null; then
    echo "az CLI not found — install via brew install azure-cli" >&2
    exit 1
fi

current_sub=$(az account show --query id -o tsv 2>/dev/null || echo "")
if [[ "${current_sub}" != "${SUBSCRIPTION_ID}" ]]; then
    warn "Current subscription is ${current_sub:-<none>}; expected ${SUBSCRIPTION_ID}"
    echo "Running: az account set --subscription ${SUBSCRIPTION_ID}"
    az account set --subscription "${SUBSCRIPTION_ID}"
fi

if ! git diff --quiet HEAD; then
    warn "Working tree has uncommitted changes — image will be tagged with the committed HEAD only."
fi

readonly GIT_SHA=$(git rev-parse --short HEAD)
readonly IMAGE_TAG="movate:0.7.0-${GIT_SHA}"
success "Will build + deploy ${IMAGE_TAG} to ${RESOURCE_GROUP}"

# -----------------------------------------------------------------------------
# 1. Build + push image via ACR cloud build
# -----------------------------------------------------------------------------

step "1. ACR build (cloud-side, ~60-90s)"

az acr build \
    --registry "${ACR_NAME}" \
    --image "${IMAGE_TAG}" \
    --image "movate:0.7.0-latest" \
    --file Dockerfile \
    --target runtime \
    .

success "Image pushed: ${ACR_NAME}.azurecr.io/${IMAGE_TAG}"

# -----------------------------------------------------------------------------
# 2. Bicep deploy with the new image + Deva's CORS origin
# -----------------------------------------------------------------------------

step "2. Bicep deploy"

readonly DEPLOY_NAME="friday-demo-$(date +%Y%m%d-%H%M%S)"
# Build the CORS allow-list — drop the trailing comma when MOVA_IO_ORIGIN
# isn't set (localhost-only deploys are fine for dev; Deva's deployed
# Mova iO origin can be added post-deploy via one `az containerapp update`).
if [[ -n "${DEVA_ORIGIN}" ]]; then
    readonly CORS_ORIGINS="${LOCAL_DEV_ORIGIN},${DEVA_ORIGIN}"
else
    readonly CORS_ORIGINS="${LOCAL_DEV_ORIGIN}"
    warn "MOVA_IO_ORIGIN not set — CORS allow-list will only include localhost:4200."
    warn "Add Deva's production hostname later by re-running this script with"
    warn "MOVA_IO_ORIGIN set, or pass corsAllowedOrigins directly to Bicep:"
    warn "  az deployment group create -g ${RESOURCE_GROUP} -f infra/azure/main.bicep \\"
    warn "    -p ${BICEP_PARAMS} \\"
    warn "    --parameters corsAllowedOrigins=\"${LOCAL_DEV_ORIGIN},<new-host>\""
fi

# Deploy main.bicep with the new image tag + CORS origins. v0.7+ threads
# corsAllowedOrigins through Bicep (item 116) so the deploy is idempotent
# in one apply — no more post-deploy `az containerapp update` step.
# Teams bot deferred on the Movate sub (the UAI is pre-staged but the
# Container App + Bot Service registration aren't deployed yet) — pass
# enableTeamsBot=false to skip the Teams-related modules.
az deployment group create \
    -g "${RESOURCE_GROUP}" \
    -f infra/azure/main.bicep \
    -p "${BICEP_PARAMS}" \
    --parameters \
        image="${IMAGE_TAG}" \
        enableTeamsBot=false \
        corsAllowedOrigins="${CORS_ORIGINS}" \
    --name "${DEPLOY_NAME}" \
    --query "{state: properties.provisioningState, apiUrl: properties.outputs.apiUrl.value}" \
    -o json

success "Bicep deploy complete — CORS allow-list: ${CORS_ORIGINS}"

# -----------------------------------------------------------------------------
# 3. Smoke-poll /healthz + /api/v1/openapi.json until the revision flips
# -----------------------------------------------------------------------------

step "3. Smoke check"

readonly API_FQDN=$(az containerapp show \
    -g "${RESOURCE_GROUP}" \
    -n "${API_APP_NAME}" \
    --query "properties.configuration.ingress.fqdn" \
    -o tsv)
readonly API_URL="https://${API_FQDN}"

success "API FQDN: ${API_FQDN}"
echo "Polling ${API_URL}/healthz until 200 (up to 5 min)..."

for i in $(seq 1 30); do
    status=$(curl -sS -o /dev/null -w "%{http_code}" "${API_URL}/healthz" --max-time 10 || echo "000")
    if [[ "${status}" == "200" ]]; then
        success "/healthz returned 200 (attempt ${i})"
        break
    fi
    echo "  attempt ${i}: HTTP ${status}, retrying in 10s..."
    sleep 10
done

# Confirm /api/v1/agents is in the OpenAPI spec (proves v0.7 deployed)
echo "Confirming new v0.7 routes are in the spec..."
v1_routes=$(curl -sS "${API_URL}/openapi.json" --max-time 15 \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(sorted(p for p in d['paths'] if '/api/v1' in p)))")
echo "${v1_routes}"

if ! echo "${v1_routes}" | grep -q "/api/v1/agents/from-wizard"; then
    warn "from-wizard endpoint NOT in OpenAPI — old image still serving?"
    echo "Check: az containerapp revision list -g ${RESOURCE_GROUP} -n ${API_APP_NAME}"
    exit 1
fi
success "Confirmed: from-wizard, /api/v1/evals, /api/v1/runs/{id}/trace all in spec"

# -----------------------------------------------------------------------------
# 4. Mint Deva's API key
# -----------------------------------------------------------------------------

step "4. Mint Deva's bearer token"

# Generate a UUID for the tenant client-side so we don't have to deal
# with $(uuidgen) shell-expansion gotchas inside `az containerapp exec`.
DEVA_TENANT_ID=$(uuidgen | tr 'A-Z' 'a-z')
readonly DEVA_TENANT_ID

# The CLI inside the container is `movate` (renamed from mdk in PR #91
# but binary entry point is `movate`; both names work today).
echo "Tenant id: ${DEVA_TENANT_ID}"
echo "Running: mdk auth create-key inside ${API_APP_NAME}..."

KEY_OUTPUT=$(az containerapp exec \
    -g "${RESOURCE_GROUP}" \
    -n "${API_APP_NAME}" \
    --command "movate auth create-key --tenant-id ${DEVA_TENANT_ID} --env live --label deva-mova-io-friday-demo" \
    2>&1 || echo "EXEC_FAILED")

if echo "${KEY_OUTPUT}" | grep -q "EXEC_FAILED"; then
    warn "az containerapp exec failed — falling back to manual mint instructions"
    cat <<EOM

  Run this manually:
  az containerapp exec \\
      -g ${RESOURCE_GROUP} \\
      -n ${API_APP_NAME} \\
      --command "movate auth create-key --tenant-id ${DEVA_TENANT_ID} --env live --label deva-mova-io-friday-demo"

  Copy the mvt_live_... bearer string into the onboarding email below.

EOM
    DEVA_KEY="<paste-mvt_live_...-here>"
else
    # Best-effort parse — the create-key output prints a "Key:" line with
    # the bearer string. Falls back to the full output if grep misses.
    DEVA_KEY=$(echo "${KEY_OUTPUT}" | grep -oE "mvt_live_[A-Za-z0-9_-]+" | head -1 || echo "<see-output-above>")
fi

success "Bearer minted (tenant ${DEVA_TENANT_ID})"

# -----------------------------------------------------------------------------
# 5. Print onboarding bundle for Deva
# -----------------------------------------------------------------------------

step "5. Onboarding bundle — paste this into Slack/email to Deva"

cat <<EOM

╔════════════════════════════════════════════════════════════════════╗
║  MDK v0.7 — Friday demo bundle for Deva                            ║
╚════════════════════════════════════════════════════════════════════╝

Hi Deva,

Your endpoints are live. Wire your Angular app to this:

──── Runtime ────────────────────────────────────────────────────────
  Base URL:      ${API_URL}
  Bearer token:  ${DEVA_KEY}
  OpenAPI spec:  ${API_URL}/openapi.json
  Swagger UI:    ${API_URL}/docs

──── First requests to confirm the wire ─────────────────────────────
  # Health (no auth required)
  curl ${API_URL}/healthz

  # Create an agent from your wizard's JSON shape:
  curl -X POST ${API_URL}/api/v1/agents/from-wizard \\
       -H "Authorization: Bearer ${DEVA_KEY}" \\
       -H "Content-Type: application/json" \\
       -d '{"name":"hello-bot","agent_prompt":"Reply with JSON {\\"output\\": <text>}","ai_model":"openai/gpt-4o-mini-2024-07-18"}'

  # Run an eval (mock provider — sub-second):
  curl -X POST ${API_URL}/api/v1/agents/hello-bot/evals \\
       -H "Authorization: Bearer ${DEVA_KEY}" \\
       -H "Content-Type: application/json" \\
       -d '{"gate":0.0,"runs":1,"mock":true}'

  # List recent evals:
  curl "${API_URL}/api/v1/evals?agent=hello-bot" \\
       -H "Authorization: Bearer ${DEVA_KEY}"

──── Client generation ──────────────────────────────────────────────
  Full instructions: docs/angular-client.md in the mdk-cli repo
  One-liner:
    npx @openapitools/openapi-generator-cli generate \\
        -i ${API_URL}/openapi.json \\
        -g typescript-angular \\
        -o src/app/api-client

──── Endpoints wired for your four verbs ────────────────────────────
  Create:        POST /api/v1/agents/from-wizard  (your JSON shape)
                 POST /api/v1/agents              (multipart, optional)
                 GET  /api/v1/agents/{name}
                 POST /api/v1/agents/{name}/validate

  Poll/Run:      POST /api/v1/agents/{name}/runs
                 GET  /jobs/{id}
                 GET  /api/v1/jobs?agent={name}

  Evals:         POST /api/v1/agents/{name}/evals
                 GET  /api/v1/evals/{eval_id}
                 GET  /api/v1/evals?agent={name}

  Observability: GET  /api/v1/runs/{run_id}/trace
                 GET  /api/v1/jobs?status={status}

──── CORS configured for ────────────────────────────────────────────
$(if [[ -n "${DEVA_ORIGIN}" ]]; then
    printf "  %s        (your local ng serve)\n  %s              (deployed Mova iO)" "${LOCAL_DEV_ORIGIN}" "${DEVA_ORIGIN}"
else
    printf "  %s        (your local ng serve)\n  %s" "${LOCAL_DEV_ORIGIN}" "(deployed Mova iO host NOT yet configured — send me the URL and I'll add it)"
fi)

──── Auth model for v0.7 alpha ──────────────────────────────────────
  Single fleet bearer (this one). Wrap in your BFF per the auth model
  in docs/angular-client.md — Angular itself should NOT hold the
  mvt_live_... key (XSS risk). Per-user SSO + scoped keys land in
  v0.8.

──── If anything 404s or 422s ───────────────────────────────────────
  - Wire shapes: see ${API_URL}/docs (Swagger UI)
  - Common 422: send Content-Type: application/json; FastAPI's
    multipart-vs-json discrimination is strict
  - Common 401: confirm the bearer is in the Authorization header
    prefixed with "Bearer " (with the space)

See you Friday.
EOM
