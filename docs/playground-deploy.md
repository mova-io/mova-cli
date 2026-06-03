# Hosted playground — end-to-end deploy runbook (ADR 053)

This walks the **first deployment** of the hosted Chainlit playground — a
**shareable, Entra-SSO-gated** test portal (`mdk playground serve`, hosted) —
into the runtime's existing Azure Container Apps environment. It implements
[ADR 053](adr/053-hosted-playground.md) Phase 1: a link you send to invited
Movate staff and Entra B2B guests, who can exercise any deployed runtime/agent
with **history persisted** and **👍/👎 feedback working** (both inherited from
the existing playground — this runbook hosts it, it does not build it).

> **Default-off.** `enablePlayground=false` (the default) ⇒ nothing changes;
> the playground app is only provisioned when you flip the flag. This is a
> **deploy-behavior** change (CLAUDE.md rule 5), additive and gated.

The cloud + IAM steps below (Entra app registration, the Key Vault secrets,
inviting B2B guests, flipping the flag) are **operator-run and supervised** —
`mdk deploy` / Bicep provisions and rolls the **app**; it does **not** silently
mutate tenant identity or access policy (ADR 053 Boundaries).

Total time on an already-deployed runtime: **~25 minutes** of clicks + waits.

---

## Prereqs

You already have:

- [x] An Azure subscription with Contributor on the target RG (Owner if you'll
  create role assignments / the RG yourself).
- [x] `az` CLI installed + `az login` succeeded.
- [x] **The runtime infra already deployed** via `mdk` /
  [infra/azure/README.md](../infra/azure/README.md) — the playground rides on
  the **same** ACR, Key Vault, Postgres, and Container Apps Environment as the
  runtime, and points at the runtime's **api** app. `enableApiWorker=true` is a
  hard requirement (the Bicep gates the playground on it).
- [x] Permission to create an Entra app registration in the tenant (or an
  identity admin who will, given the values from Step 1).

If the runtime isn't deployed yet, do that first
([infra/azure/README.md](../infra/azure/README.md)); this runbook starts where
that one ends.

Set some shell vars used throughout (adjust to your env):

```bash
ENV=dev
RG_NAME="movate-${ENV}-rg"
VAULT_NAME="movate-${ENV}-kv"            # append your nameSuffix if you set one
PLAYGROUND_NAME="movate-${ENV}-playground"
```

---

## Step 1 — pre-create the Entra app registration (one-time per env)

Easy Auth (ADR 053 D4) binds to an Entra app registration. It is
**operator-pre-created** — this runbook step, never silent automation. We don't
know the app's FQDN yet (the Container App must exist to learn it), so we create
the registration now and add the redirect URI in **Step 5**, after a first
deploy reveals the FQDN.

```bash
APP_NAME="movate-playground-${ENV}"

# Create the app registration (single-tenant by default; see note below for B2B).
APP_JSON=$(az ad app create --display-name "$APP_NAME" --output json)
ENTRA_CLIENT_ID=$(echo "$APP_JSON" | jq -r .appId)
echo "Entra client id: $ENTRA_CLIENT_ID"

# Mint a client secret — Easy Auth uses it for the OIDC code exchange.
SECRET_JSON=$(az ad app credential reset --id "$ENTRA_CLIENT_ID" \
  --display-name 'movate-playground-easyauth' --output json)
ENTRA_CLIENT_SECRET=$(echo "$SECRET_JSON" | jq -r .password)
echo "Entra client secret: (saved — shown once)"
```

Save **both** `$ENTRA_CLIENT_ID` and `$ENTRA_CLIENT_SECRET` — you paste the id
into the deploy params and the secret into Key Vault (Step 2). The secret is
shown **once**; `az ad app credential reset` mints a new one if you lose it.

> **External testers = Entra B2B guests.** To let non-Movate testers in, invite
> them as **Entra B2B guests** in the tenant (Entra portal → External
> Identities → Guest invitations, or `az ad user invite`). They authenticate
> with their own identity — no bespoke accounts. **Set a usage quota before
> sharing widely** (ADR 053 Risks — the portal spends BYOK tokens on a shared
> key). Track guests for **offboarding**; stale guests linger.

## Step 2 — populate Key Vault secrets

The playground reads three secrets from KV at startup (ADR 053 D3 / D4). Two are
playground-specific; `pg-admin-password` is the runtime's existing Postgres
secret (already present from the runtime deploy — listed here only so you can
confirm it exists).

```bash
# (a) The SCOPED runtime bearer the portal carries (ADR 053 D3 / R5).
#     LEAST-PRIVILEGE — run agents + write feedback only. NEVER a fleet-admin
#     or bootstrap key. Mint a scoped key on the deployed runtime:
#         mdk auth create-key --target ${ENV} --scopes runs:write,feedback:write
#     (use whatever flat least-privilege scopes your runtime exposes — the point
#     is "not admin"), then store it:
az keyvault secret set --vault-name "$VAULT_NAME" \
  --name playground-runtime-key --value "mvt_${ENV}_<tenant>_<keyid>_<secret>"

# (b) The Entra client secret from Step 1 — Easy Auth's OIDC client secret.
az keyvault secret set --vault-name "$VAULT_NAME" \
  --name playground-entra-client-secret --value "$ENTRA_CLIENT_SECRET"

# (c) Confirm the runtime's Postgres password secret already exists (it does if
#     the runtime is deployed) — the playground shares the runtime DB for its
#     Chainlit data layer.
az keyvault secret show --vault-name "$VAULT_NAME" \
  --name pg-admin-password --query name -o tsv
```

> **Set a quota before sharing widely.** Per ADR 053 D7 / Risks, the scoped key
> is shared across all testers, so one budget covers all of them. Configure the
> runtime's per-tenant quota (ADR 036) **before** circulating the link.

> **Rotate at will.** The app reads these on startup; restart the Container App
> after rotation (`az containerapp revision restart`).

## Step 3 — first deploy (provision the app, learn its FQDN)

Flip `enablePlayground=true` and pass the Entra client id. (Easy Auth still
needs the redirect URI added in Step 5, but the app + its FQDN come up now.)

```bash
az deployment group create \
  --resource-group "$RG_NAME" \
  --template-file infra/azure/main.bicep \
  --parameters infra/azure/main.${ENV}.bicepparam \
  --parameters enablePlayground=true playgroundEntraClientId="$ENTRA_CLIENT_ID"
```

This creates **`movate-${ENV}-playground`** — a Container App in the runtime's
env, running `mdk playground serve` with external ingress, pointed at the api
app's in-env FQDN, with the Chainlit data layer on the runtime's Postgres.

Grab its FQDN:

```bash
PLAYGROUND_URL=$(az deployment group show \
  --resource-group "$RG_NAME" --name main \
  --query 'properties.outputs.playgroundUrl.value' -o tsv)
echo "Playground URL: $PLAYGROUND_URL"
PLAYGROUND_FQDN="${PLAYGROUND_URL#https://}"
```

> If `playgroundEntraTenantId` is left empty (default), Easy Auth issues against
> the deployment's own tenant. Set it only for a cross-tenant app registration.

## Step 4 — set the per-env minReplicas (optional)

By default the playground scales to **zero** (ADR 053 D7) — an idle portal costs
nothing and cold-starts on the first authenticated request. For a demo where the
cold-start is unwelcome, keep one replica warm:

```bash
az deployment group create \
  --resource-group "$RG_NAME" \
  --template-file infra/azure/main.bicep \
  --parameters infra/azure/main.${ENV}.bicepparam \
  --parameters enablePlayground=true playgroundEntraClientId="$ENTRA_CLIENT_ID" \
               playgroundMinReplicas=1
```

## Step 5 — finish the Easy Auth redirect URI

Easy Auth's OIDC callback lives at `/.auth/login/aad/callback` on the app's
FQDN. Add it to the Entra app registration now that the FQDN is known:

```bash
az ad app update --id "$ENTRA_CLIENT_ID" \
  --web-redirect-uris "https://${PLAYGROUND_FQDN}/.auth/login/aad/callback"
```

The `authConfig` child resource (wired by the Bicep) already references the
client id + the KV-backed client secret and sets
`unauthenticatedClientAction: RedirectToLoginPage` — so the moment the redirect
URI is registered, every anonymous request is bounced to Entra login (ADR 053
R2: **public ingress, never public access**).

## Step 6 — first end-to-end smoke

1. Open `$PLAYGROUND_URL` in a fresh/incognito browser.
2. You should be **redirected to the Entra login page** (not served the app) —
   this is the guardrail working. Sign in with a tenant identity (or an invited
   B2B guest).
3. After SSO you land in the Chainlit playground. Pick an agent and send a
   message — it runs against the deployed runtime via the scoped key.
4. Click 👍 or 👎 on a reply. The feedback POSTs to the runtime's
   `POST /api/v1/runs/{run_id}/feedback` and persists (inherited behavior,
   ADR 053 D6).
5. Refresh — your conversation is still in the sidebar (history persisted to
   Postgres, ADR 053 D3).

Share `$PLAYGROUND_URL` with invited testers. **"Send me a link and I'll try
it"** now has an answer.

---

## Troubleshooting

### The page loads WITHOUT asking me to log in

Easy Auth isn't gating yet. Check the authConfig exists and the redirect URI
is registered (Step 5):

```bash
az containerapp auth show --name "$PLAYGROUND_NAME" --resource-group "$RG_NAME" -o jsonc
# globalValidation.unauthenticatedClientAction should be "RedirectToLoginPage";
# identityProviders.azureActiveDirectory.enabled should be true.
```

If the authConfig is missing, re-run the Step 3 deploy (the child resource is
created with the app). If login loops, the redirect URI in the Entra app
registration doesn't match `https://<fqdn>/.auth/login/aad/callback`.

### "AADSTS500113: No reply address registered" after login

The redirect URI wasn't added (Step 5) or doesn't match the FQDN exactly. Re-run
the `az ad app update --web-redirect-uris ...` command with the current FQDN.

### Revision won't start — "unable to fetch secret 'playground-...'"

A KV secret is missing or the playground UAI lacks read access. Confirm both
playground secrets exist (Step 2) and that the
`movate-${ENV}-playground-mi` identity has the **Key Vault Secrets User** role
on the vault (the Bicep grants this un-gated, so it should be present after any
deploy). Re-check:

```bash
az keyvault secret show --vault-name "$VAULT_NAME" --name playground-runtime-key --query name -o tsv
az keyvault secret show --vault-name "$VAULT_NAME" --name playground-entra-client-secret --query name -o tsv
```

### Agents run but feedback / history don't persist

The Chainlit data layer can't reach Postgres. Verify `pg-admin-password` exists
in KV and the runtime's Postgres firewall admits the env. The playground uses
`MDK_PLAYGROUND_THREADS_URL` (set by the Bicep to the runtime DB) + `PGPASSWORD`.

### Runtime calls 401 / 403

The scoped key is wrong or unscoped. Re-mint a least-privilege key
(`mdk auth create-key`) and update the `playground-runtime-key` KV secret, then
restart the revision. **Never** substitute a fleet-admin / bootstrap key
(ADR 053 R5).

---

## Offboarding / hygiene

- **B2B guests** (ADR 053 Risks — guest sprawl): periodically review and remove
  stale guest accounts (Entra portal → Users → filter Guest, or
  `az ad user list --filter "userType eq 'Guest'"`).
- **Quota** (ADR 053 D7): keep a per-tenant spend ceiling on the scoped key's
  tenant before widening the audience.
- **Scale-to-zero** is the default; leave `playgroundMinReplicas` at its `-1`
  sentinel unless a demo needs a warm replica.

## What this runbook does NOT do (Phase 2 — separate PRs, ADR 053 D6)

- Aggregate-feedback dashboard panel.
- 👎 → eval-harvest wiring (ADR 016).
- Per-tester identity on feedback rows from the Easy-Auth `X-MS-CLIENT-PRINCIPAL`.

None of these gate the Phase-1 shareable-URL milestone.
