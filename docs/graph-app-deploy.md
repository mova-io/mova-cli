# Hosted knowledge-graph dashboard — deploy runbook (ADR 081)

Deploys the **hosted knowledge-graph dashboard** — a shareable, Entra-SSO-gated
viewer for the GraphRAG knowledge graph (`mdk graph dashboard`, hosted) — into
the runtime's existing Azure Container Apps environment.
Implements [ADR 081](adr/081-hosted-knowledge-graph-dashboard.md).

The graph **query API already ships inside the api app** (ADR 046). This runbook
only hosts the **viewer**; it does not build the graph, the API, or the viewer
assets. It closely mirrors the [playground runbook](playground-deploy.md) — same
Easy-Auth + Key-Vault ceremony, one fewer secret (no Postgres data layer).

> **Default-off.** `enableGraphApp=false` (the default) ⇒ nothing changes; the
> app is only provisioned when you flip the flag. Additive, gated deploy-behavior
> change (CLAUDE.md rule 5).

The Entra app registration, the Key Vault secrets, and flipping the flag are
**operator-run** — Bicep provisions/rolls the **app**; it never silently mutates
tenant identity or access policy.

Total time on an already-deployed runtime: **~20 minutes** of clicks + waits.

---

## Prereqs

- [x] Azure subscription with Contributor on the target RG (Owner to create role
  assignments).
- [x] `az` CLI + `az login`.
- [x] **The runtime infra already deployed** with `enableApiWorker=true` — the
  viewer rides the **same** ACR, Key Vault, and Container Apps Environment as the
  runtime and proxies the runtime's **api** app (the Bicep gates the viewer on
  `enableApiWorker`).
- [x] Permission to create an Entra app registration in the tenant.

Shell vars used throughout:

```bash
ENV=dev
RG_NAME="movate-${ENV}-rg"
VAULT_NAME="movate-${ENV}-kv"            # append your nameSuffix if you set one
GRAPH_NAME="movate-${ENV}-graph"
```

---

## Step 1 — Mint a read-scoped runtime key

The viewer calls the graph API server-side with a **least-privilege, read-scoped**
bearer (the graph API only needs `read`). Mint one with the runtime's key tooling
(see your API-key runbook), scope it to `read`, then store it:

```bash
az keyvault secret set --vault-name "$VAULT_NAME" \
  --name graph-runtime-key --value "mvt_live_...<read-scoped key>"
```

> Never use a fleet-admin / bootstrap key here. If the app is compromised, this
> key bounds the blast radius to read-only graph access for its tenant.

## Step 2 — Create the Entra app registration (Easy Auth)

```bash
az ad app create --display-name "movate-graph-${ENV}" \
  --web-redirect-uris "https://PLACEHOLDER/.auth/login/aad/callback"
# note the printed appId → that's graphEntraClientId
```

The redirect URI needs the app's FQDN, which you only learn after the first
deploy. So: deploy once (Step 4) to learn the FQDN, then come back and **update**
the redirect URI to the real one:

```bash
az ad app update --id <appId> \
  --web-redirect-uris "https://${GRAPH_NAME}.<env-default-domain>/.auth/login/aad/callback"
```

Create a client secret for the registration and store it:

```bash
az ad app credential reset --id <appId> --append --query password -o tsv
az keyvault secret set --vault-name "$VAULT_NAME" \
  --name graph-entra-client-secret --value "<the-secret>"
```

## Step 3 — Set the parameters

In your `main.<env>.bicepparam`:

```bicep
param enableGraphApp = true
param graphEntraClientId = '<appId from Step 2>'
// param graphEntraTenantId = ''      // empty → the deployment's own tenant
// param graphProjectId = 'acme-kb'   // optional: seed the initial project
// param graphMinReplicas = 1         // optional: keep warm (default scale-to-zero)
```

## Step 4 — Deploy

```bash
az deployment group create -g "$RG_NAME" \
  -f infra/azure/main.bicep -p infra/azure/main.${ENV}.bicepparam \
  --query "properties.outputs.graphUrl.value" -o tsv
```

The printed `graphUrl` is the shareable, SSO-gated link. On the **first** deploy,
do Step 2's redirect-URI update now that you know the FQDN, then re-deploy (or
just `az containerapp update` a new revision) so Easy Auth accepts logins.

## Step 5 — Verify

```bash
# App is running:
az containerapp show -n "$GRAPH_NAME" -g "$RG_NAME" \
  --query "properties.runningStatus" -o tsv      # → Running / RunningAtMaxScale

# Browse: open graphUrl → Entra login → the dashboard loads.
# Logs (expect the dashboard server bound on 0.0.0.0:8901, no config errors):
az containerapp logs show -n "$GRAPH_NAME" -g "$RG_NAME" --tail 30
```

If the dashboard shows a "graph API not available" banner, the target runtime
predates ADR 046 — check `mdk capabilities` against it.

---

## Local alternative (no hosting)

You don't need this app to browse the graph — any operator with the CLI can run
the viewer locally against a deployed runtime:

```bash
export MOVATE_DEV_KEY=mvt_live_...        # read-scoped
mdk graph dashboard --target dev --project acme-kb
```

The hosted app exists to give **non-CLI** stakeholders a shareable SSO link.

## Teardown

Flip `enableGraphApp=false` and re-deploy → the app + its authConfig are removed
(the UAI + role assignments are cheap and remain for a clean re-enable). Revoke
the `graph-runtime-key` and delete the Entra app registration if retiring it.
