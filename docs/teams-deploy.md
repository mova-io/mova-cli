# Teams bot — end-to-end deploy runbook

This walks the **first deployment** of the Teams bot from a clean
Azure subscription to a working Teams app catalog entry. Same Azure
tenant is fine for prod or dev — the Bicep is parameterised.

> **For early-pilot** (testing against a personal Azure account
> while the Movate-tenant migration is in flight), every step below
> works unchanged. The artifacts (appPackage, Bot Service, AAD app
> id) port to the Movate tenant when migration lands — only the
> resource-group + subscription-id change.

Total time on a fresh sub: **~30 minutes** of clicks + waits.

---

## Prereqs

You already have:

- [x] An Azure subscription with Contributor on a target RG (or
  Owner if you're going to create the RG yourself)
- [x] `az` CLI installed + `az login` succeeded
- [x] The runtime infra deployed via `mdk` (see
  [infra/azure/README.md](../infra/azure/README.md)). The Teams bot
  rides on the **same** ACR, Key Vault, and Container Apps
  Environment as the runtime.

If the runtime isn't deployed yet: run the steps in
[infra/azure/README.md](../infra/azure/README.md) first. They take
~15 minutes; this runbook starts where that one ends.

## Step 1 — create the AAD app (one-time per env)

The Bot Service resource needs a bot AAD app id at creation time.
Bot Service binds to it; it's not updatable later.

```bash
APP_NAME="movate-teams-bot-dev"

# Create the AAD app + service principal.
APP_JSON=$(az ad app create --display-name "$APP_NAME" --output json)
APP_ID=$(echo "$APP_JSON" | jq -r .appId)
echo "AAD app id: $APP_ID"

# Mint a client secret.
SECRET_JSON=$(az ad app credential reset --id "$APP_ID" --display-name 'movate-teams-bot' --output json)
APP_PASSWORD=$(echo "$SECRET_JSON" | jq -r .password)
echo "AAD app password: $APP_PASSWORD"
```

Save BOTH the `appId` and the `password` somewhere safe — you'll
paste them into Key Vault in step 2. The password is shown ONCE;
re-running `az ad app credential reset` generates a new one.

## Step 2 — populate Key Vault secrets

The Teams bot reads three secrets from KV at startup:

```bash
VAULT_NAME="movate-dev-kv"

# Fleet API key — mint via `mdk auth create-key` on the deployed runtime.
az keyvault secret set --vault-name "$VAULT_NAME" \
  --name movate-teams-fleet-api-key --value "mvt_dev_<tenant>_<keyid>_<secret>"

# Fernet encryption key — for the per-user identity-binding store (3.1.c).
ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
az keyvault secret set --vault-name "$VAULT_NAME" \
  --name movate-teams-encryption-key --value "$ENCRYPTION_KEY"

# Bot Service AAD app password (the JWT-validation hardening PR will consume this).
az keyvault secret set --vault-name "$VAULT_NAME" \
  --name microsoft-app-password --value "$APP_PASSWORD"
```

> **Rotate at will.** The bot reads these on startup; restart the
> Container App after rotation (`az containerapp revision restart`).

## Step 3 — deploy the bot infra

Bicep flips `enableTeamsBot=true` and passes `teamsBotAppId`:

```bash
RG_NAME="movate-dev-rg"

az deployment group create \
  --resource-group "$RG_NAME" \
  --template-file infra/azure/main.bicep \
  --parameters infra/azure/main.dev.bicepparam \
  --parameters enableTeamsBot=true teamsBotAppId="$APP_ID"
```

The deployment creates:

- **`movate-dev-teams-bot`** Container App — runs `mdk teams-bot serve`
- **`movate-dev-bot`** Bot Service registration (global resource) — wires Teams as a channel

Watch the deployment output for `teamsBotWebhookUrl` — that's the
HTTPS endpoint Teams will POST to. Smoke test it:

```bash
WEBHOOK_URL=$(az deployment group show \
  --resource-group "$RG_NAME" --name main \
  --query 'properties.outputs.teamsBotWebhookUrl.value' -o tsv)

# Strip /api/messages → just the base URL → /health.
HEALTH_URL="${WEBHOOK_URL%/api/messages}/health"
curl -sf "$HEALTH_URL" | jq .
# → {"status":"ok","service":"movate-teams-bot"}
```

## Step 4 — build the Teams app package

```bash
MOVATE_TEAMS_BOT_APP_ID="$APP_ID" \
MOVATE_TEAMS_VALID_DOMAINS="movate.com,langfuse.movate.com" \
  ./scripts/teams-package.sh

# → dist/movate-teams.zip
```

The script substitutes the AAD app id into the manifest at build
time. The default placeholder UUID would be rejected by Teams
Admin Center — the warning surfaces if you forget to set the env.

## Step 5 — upload to Teams

**For a single-user pilot (sideload):**

1. Open Teams desktop → Apps → Manage your apps → Upload an app → Upload a custom app
2. Pick `dist/movate-teams.zip`
3. Teams installs the bot in your personal scope. `@movate ping` should return `pong` in under 2 seconds.

**For the whole org:**

1. https://admin.teams.microsoft.com → Teams apps → Manage apps → Upload new app
2. Pick `dist/movate-teams.zip` and approve
3. App appears in the org-wide app catalog after Teams indexes it (~15-30 minutes)

## Step 6 — first end-to-end smoke

Three commands in a Teams DM with the bot:

```
@movate ping
→ pong

/movate connect mvt_dev_<your-tenant>_<keyid>_<secret>
→ ✓ Connected to tenant <X>

@movate run faq-agent {"question":"what is movate?"}
→ Rendered Adaptive Card with response + cost + latency
```

If any of these fail, see "Troubleshooting" below.

## Troubleshooting

### `@movate ping` times out

Check the Bot Service can reach the Container App:

```bash
az bot show --name movate-dev-bot --resource-group "$RG_NAME" \
  --query 'properties.endpoint' -o tsv
# Should match teamsBotWebhookUrl from the deployment output.

# Hit the Container App's health endpoint from your laptop.
curl -v "${WEBHOOK_URL%/api/messages}/health"
# Should return 200 with the service tag.
```

If `/health` works but Bot Service can't reach the bot: check the
Bot Service's "Channels" tab in the Azure portal for any red error
indicators on the Teams channel.

### "couldn't fetch your stored key"

The encryption key rotated since the user's `/movate connect`. Tell
the user to DM `/movate connect <new-key>` to rebind.

### "Manifest validation failed" on Teams Admin Center upload

Most common: the manifest's `id` is still the placeholder UUID.
Re-run `scripts/teams-package.sh` with
`MOVATE_TEAMS_BOT_APP_ID=$APP_ID` set.

Second-most-common: the placeholder icons (committed under
`appPackage/icons/`) are too small for production publishing.
Replace them with real Movate-branded artwork before going past
sideload mode. The placeholder is fine for personal-scope upload.

### Bot Service won't create — "tenant mismatch"

The bot's AAD app and the Bot Service must live in the same tenant.
Check `az account show` and `az ad app show --id "$APP_ID"` agree on
`tenantId`.

### Health endpoint returns 503

The bot can't reach the runtime (`MOVATE_RUNTIME_URL` wrong) OR the
runtime is down. Verify:

```bash
mdk doctor --target dev   # walks api + worker /healthz
```

## Production checklist

Before flipping `sku: 'F0'` → `'S1'` (paid tier) and publishing
org-wide:

- [ ] Real PNG icons replace the placeholders (see `appPackage/README.md`)
- [ ] `validDomains` in the manifest lists every URL the bot's cards
  can deep-link to (Langfuse, your trace viewer, etc.)
- [ ] JWT validation lands ([issue #70](https://github.com/mova-io/mova-cli/issues/70))
- [ ] Privacy + terms URLs in the manifest point at the right Movate-internal pages
- [ ] One sales-engineer pilot for a full week, observing real run
  patterns + cost
- [ ] Per-tenant binding (`MOVATE_TEAMS_REQUIRE_BINDING=1`) if the
  audience extends beyond Movate-internal employees

## Open questions tracked separately

- **Microsoft Graph auth for native Teams file uploads.** Bot
  Framework Emulator's `file://` URLs work today; the Graph URL
  used by Teams native attachments needs the bot's AAD token.
  Tracked in the 3.1.d follow-up.
- **Run-the-uploaded-agent.** Today the upload validates an agent
  bundle but doesn't register it with the runtime. The runtime's
  `/run` endpoint takes an agent NAME registered via
  `mdk serve --agents-path`, not an inline bundle. Adding an
  inline-bundle endpoint is a separate runtime API PR.
- **Production tenant migration to Movate Azure.** The bootstrap
  above works against any subscription. Migration to the Movate
  tenant is mechanical once Movate IT provisions access — same
  Bicep, different `--subscription` + `--resource-group`. Tracked
  in [#65](https://github.com/mova-io/mova-cli/issues/65).
