# Movate Azure — MDK runtime architecture

**Subscription:** `AZLABSV2.0-Sandbox(POC)` (`8fab0f8f-b577-45d7-a485-ec32f73b22be`)
**Resource group:** `movate-dev-rg` (East US 2)
**Live since:** 2026-05-14 (migrated from a personal pay-as-you-go sub on the same day)
**Runtime URL:** https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io
**OpenAPI:** https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io/api/v1/openapi.json

This doc is the standalone reference for what runs on the Movate Azure
sub. The migration runbook (`docs/azure-movate-migration-runbook.md`)
covers HOW we got here; this one covers WHAT'S THERE for anyone who
needs to operate, extend, or hand off ownership.

## Resources

All in `movate-dev-rg`. Names use a `-mvt` suffix on the globally-
unique ones (KV, ACR, Postgres) so they don't collide with anything
else in the Azure-wide namespace.

| # | Resource | Type | Purpose |
|---|---|---|---|
| 1 | `movate-dev-api` | Container App | The FastAPI HTTP runtime. Public ingress on :8000. Serves `/healthz`, `/ready`, `/api/v1/agents/*`, `/api/v1/openapi.json`. |
| 2 | `movate-dev-worker` | Container App | Background job worker. Claims jobs from Postgres, executes against LLM providers, persists runs. Internal-only. |
| 3 | `movate-dev-cae` | Container Apps Environment | Hosts both Container Apps above; provides shared networking + log routing. |
| 4 | `movatedevacrmvt` | Container Registry (Basic) | Holds the `movate:0.7.0-<sha>` images both apps pull from. |
| 5 | `movate-dev-kv-mvt` | Key Vault | Stores 6 runtime secrets (see below). |
| 6 | `movate-dev-pg-mvt` | Postgres Flexible Server (Burstable B1ms / 32 GB) | Holds runs, jobs, api_keys, evals, workflow_runs. |
| 7 | `movate-dev-logs` | Log Analytics Workspace (30-day retention) | Receives logs + metrics from every Container App. |
| 8 | `movate-dev-api-mi` | User-Assigned Managed Identity | Identity the API app authenticates as for ACR pulls + KV reads. |
| 9 | `movate-dev-worker-mi` | User-Assigned Managed Identity | Same, for the worker app. |
| 10 | `movate-dev-teams-bot-mi` | User-Assigned Managed Identity | Pre-staged for the Teams bot (not yet deployed; placeholder for a v0.8 follow-up). |

### Key Vault secrets

The 6 secrets in `movate-dev-kv-mvt`. Container Apps reference them via
`secretRef` so values never appear in pod env, deployment outputs, or
ARM history.

| Secret name | Reader | Source / rotation |
|---|---|---|
| `pg-admin-password` | api-mi, worker-mi | Generated at deploy time via `openssl rand`. Rotate quarterly. |
| `pg-connection-string` | api-mi, worker-mi | Constructed from FQDN + admin password at deploy time. Rotate when the password rotates. |
| `openai-api-key` | api-mi, worker-mi | OpenAI dashboard. Rotate per OpenAI's recommendation. |
| `anthropic-api-key` | api-mi, worker-mi | Anthropic console. |
| `langfuse-secret-key` | api-mi, worker-mi | Langfuse project settings. |
| `langfuse-public-key` | api-mi, worker-mi | Langfuse project settings (paired with `secret-key`). |

### Role assignments

Six role assignments wire identities to resources. All created by Bicep
in `infra/azure/main.bicep`.

| Identity | Role | Scope |
|---|---|---|
| `api-mi` | AcrPull | `movatedevacrmvt` |
| `worker-mi` | AcrPull | `movatedevacrmvt` |
| `teams-bot-mi` | AcrPull | `movatedevacrmvt` |
| `api-mi` | Key Vault Secrets User | `movate-dev-kv-mvt` |
| `worker-mi` | Key Vault Secrets User | `movate-dev-kv-mvt` |
| `teams-bot-mi` | Key Vault Secrets User | `movate-dev-kv-mvt` |

The SP that runs deploys (`fe9e2bf7-e212-4c70-a153-19e7c8a98269`) holds
`Contributor` + `User Access Administrator` at the subscription scope.
Contributor handles every resource-write action; UAA handles the role
assignments above. Without UAA, the deploy fails at the role-assignment
step (we hit this on the first migration attempt and had to escalate).

## Wiring

```
External callers
(Mova iO Angular, curl, CI)
        │
        │  HTTPS + Bearer (mvt_live_...)
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  movate-dev-cae  (Container Apps Env)               │
│  ───────────────────────────────────────────────────────────────    │
│                                                                     │
│   ┌────────────────────────┐        ┌────────────────────────┐      │
│   │ movate-dev-api         │        │ movate-dev-worker      │      │
│   │  (Container App, public│        │  (Container App,       │      │
│   │   ingress :8000)       │        │   internal)            │      │
│   │  identity: api-mi      │        │  identity: worker-mi   │      │
│   └───────┬──────────┬─────┘        └─────┬──────────┬───────┘      │
│           │          │                    │          │              │
│           │          └──── queue ────────►│          │              │
│           │            (Postgres rows)    │          │              │
│           │                               │          │              │
└───────────┼───────────────────────────────┼──────────┼──────────────┘
            │                               │          │
            │  ACR pull                     │          │  outbound HTTPS
            ▼  (via MI, AcrPull role)       ▼          ▼
   ┌──────────────────┐         ┌──────────────────┐  ┌──────────────────┐
   │  movatedevacrmvt │         │ movate-dev-kv-mvt│  │  openai.com      │
   │  (ACR)           │         │  (Key Vault)     │  │  anthropic.com   │
   │  movate:0.7.0-X  │         │   6 secrets:     │  │  langfuse cloud  │
   └──────────────────┘         │  • openai-api-key│  └──────────────────┘
            ▲                   │  • anthropic-... │
            │                   │  • langfuse-...  │
            │  (worker also     │  • pg-admin-pw   │
            │   pulls image)    │  • pg-conn-str   │
            │                   └──────────────────┘
            │                            ▲
            │                            │  KV secret reads
            │                            │  (via MI, Secrets User)
            │                            │
            │                   ┌──────────────────────────┐
            │                   │ movate-dev-pg-mvt        │
            └──── worker also   │  (Postgres Flexible)     │
                 reads KV ─────►│   jobs, runs, evals,     │
                                │   api_keys, ...          │
                                └──────────────────────────┘
                                            ▲
                                            │  TCP 5432, sslmode=require
                                            │
                            both apps ─────►│
                                            │
       ┌────────────────────────────────────┴────────────────────┐
       │                                                         │
       │  All container logs/metrics ──────►  movate-dev-logs    │
       │                                       (Log Analytics)   │
       └─────────────────────────────────────────────────────────┘
```

## Request lifecycle (worked example)

```
Mova iO Angular UI                MDK API                MDK Worker        OpenAI
        │                            │                         │              │
        │  POST /api/v1/agents/      │                         │              │
        │   faq-agent/runs           │                         │              │
        │   + Bearer mvt_live_...    │                         │              │
        │ ──────────────────────────►│                         │              │
        │                            │ verify bearer vs        │              │
        │                            │  api_keys table         │              │
        │                            │ ───┐                    │              │
        │                            │ ◄──┘                    │              │
        │                            │                         │              │
        │                            │ INSERT jobs(status=     │              │
        │                            │  queued) → job_id       │              │
        │                            │                         │              │
        │  202 {job_id, status:queued}│                        │              │
        │ ◄──────────────────────────│                         │              │
        │                            │                         │              │
        │                            │       (worker poll)     │              │
        │                            │                         │  UPDATE      │
        │                            │                         │   jobs       │
        │                            │                         │   SET status │
        │                            │                         │     =running │
        │                            │                         │              │
        │                            │                         │  read agent  │
        │                            │                         │   bundle     │
        │                            │                         │   from disk  │
        │                            │                         │              │
        │                            │                         │  read OpenAI │
        │                            │                         │   key from   │
        │                            │                         │   KV         │
        │                            │                         │              │
        │                            │                         │  POST        │
        │                            │                         │  /v1/chat/   │
        │                            │                         │  completions │
        │                            │                         │ ────────────►│
        │                            │                         │              │
        │                            │                         │ ◄────────────│
        │                            │                         │  response +  │
        │                            │                         │  usage       │
        │                            │                         │              │
        │                            │                         │  validate    │
        │                            │                         │   response   │
        │                            │                         │   vs schema  │
        │                            │                         │              │
        │                            │                         │  INSERT runs │
        │                            │                         │  UPDATE jobs │
        │                            │                         │   status=    │
        │                            │                         │   success    │
        │                            │                         │              │
        │  GET /jobs/{job_id}        │                         │              │
        │  (polling, 1s interval)    │                         │              │
        │ ──────────────────────────►│                         │              │
        │                            │ SELECT jobs WHERE       │              │
        │                            │  job_id=...             │              │
        │                            │ ───┐                    │              │
        │                            │ ◄──┘                    │              │
        │  200 {status:success,      │                         │              │
        │   result_run_id}           │                         │              │
        │ ◄──────────────────────────│                         │              │
        │                            │                         │              │
        │  GET /runs/{run_id}        │                         │              │
        │ ──────────────────────────►│                         │              │
        │                            │ SELECT runs WHERE ...   │              │
        │                            │ ───┐                    │              │
        │                            │ ◄──┘                    │              │
        │  200 {output, metrics}     │                         │              │
        │ ◄──────────────────────────│                         │              │
        │                            │                         │              │
```

Typical latency: 2-4 seconds for a small prompt against `gpt-4o-mini`,
dominated by the LLM call. Queue overhead is ~500ms.

The `?wait=true` query param on the run endpoint sidesteps the worker
entirely — the API pod executes the run synchronously and returns the
result in one round-trip. Useful for wizard-created agents on the API
pod's filesystem (the worker can't see them due to ACA's pod-local
filesystems; cross-pod sync is BACKLOG item 109).

## Per-environment scale

| Aspect | Dev (current) | Prod (future) |
|---|---|---|
| API replicas | 1 min / 2 max | 2 min / 10 max |
| Worker replicas | 1 min / 2 max | 2 min / 20 max |
| API CPU/RAM | 0.5 vCPU / 1.0 GiB | 1.0 vCPU / 2.0 GiB |
| Postgres SKU | Burstable B1ms / 32 GB | GeneralPurpose D2ds_v5 / 64 GB |
| ACR SKU | Basic | Standard |
| Log retention | 30 days | 90 days |
| Worker scale trigger | Queue depth 3/replica | Queue depth 10/replica |

All defaults live in `infra/azure/main.bicep`; flip `param env` to
`staging` or `prod` and the defaults adjust automatically.

## Cost estimate (dev tier, steady state)

| Line item | Approx /month |
|---|---|
| Postgres Burstable B1ms + 32GB storage | $13 |
| Log Analytics (Pay-as-you-go, ~1 GB/mo) | $3 |
| Container Apps consumption | $5-15 (varies by traffic) |
| ACR Basic | $5 |
| Key Vault (transactions) | <$1 |
| Container Apps Env | $0 (consumption-based) |
| **Total infrastructure** | **~$25-40 / month** |
| LLM calls (OpenAI / Anthropic) | pass-through, billed separately |

## What's NOT on this sub yet

Deliberately scoped out of the v0.7 migration; will land on follow-ups:

- **Teams bot Container App + Bot Service registration.** Pre-staged
  the `teams-bot-mi` identity but didn't deploy the app (`enableTeamsBot
  = false` in the bicepparam). Re-enable when ops decides Teams is in
  scope for Movate Azure rather than the personal sub.
- **Custom domain / vanity URL.** Currently using the Azure-generated
  `*.azurecontainerapps.io` FQDN. Adding a custom DNS + cert is a
  follow-up if/when product wants `api.mova-io.movate.com` style URLs.
- **Multi-region or staging environment.** Single-region (East US 2)
  for dev. `staging` / `prod` envs are documented in the Bicep but not
  yet provisioned on this sub.
- **CI/CD wired against this sub.** Today's deploys run from a local
  laptop via the SP. Wiring `friday-demo-deploy.sh` (or a GitHub Actions
  equivalent) to deploy from CI is a v0.8 follow-up.

## Operator quick reference

```bash
# Switch az context to this sub
set -a; source ~/.movate/azure.env; set +a
az login --service-principal -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID"
az account set --subscription 8fab0f8f-b577-45d7-a485-ec32f73b22be

# Smoke the runtime
curl -s https://movate-dev-api.bluebush-9aec1e70.eastus2.azurecontainerapps.io/healthz

# Tail the API container's logs
az containerapp logs show -g movate-dev-rg -n movate-dev-api --tail 100

# Tail the worker container's logs
az containerapp logs show -g movate-dev-rg -n movate-dev-worker --tail 100

# Shell into the API container (TTY workaround for the Bash tool)
script -q /dev/null az containerapp exec -g movate-dev-rg -n movate-dev-api

# Re-deploy a new image tag (interactive)
az acr build --registry movatedevacrmvt --image movate:0.7.0-<sha> -f Dockerfile --target runtime .
az deployment group create -g movate-dev-rg \
    -f infra/azure/main.bicep \
    -p infra/azure/main.movate.bicepparam \
    --parameters image=movate:0.7.0-<sha>
```

## Related docs

- `docs/azure-movate-migration-runbook.md` — the 12-step blue/green migration that produced this stack.
- `docs/azure-credentials-setup.md` — the three-files convention for storing the SP credentials.
- `docs/deva-friday-demo.md` — the demo script the runtime supports.
- `infra/azure/main.bicep` — source of truth for everything provisioned above.
- `BACKLOG.md` — outstanding work (Teams bot, custom domain, etc.).
