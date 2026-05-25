# mdk — MVP Workflow & Fundamentals

A practical, end-to-end guide for a new engineer on the team: the mental model,
how authentication works, and the **build → deploy → test → iterate** loop that
is the heart of the product.

> `mdk` is the CLI. `movate` is a legacy alias for the same binary — they're
> interchangeable; this guide uses `mdk`. Every command has `--help`.

---

## 0. Mental model (read this first)

mdk has **two planes**, deliberately kept separate:

| Plane | What it is | You touch it via |
| --- | --- | --- |
| **Control plane** | Authoring, validating, deploying, operating | the `mdk` CLI on your laptop |
| **Execution plane** | The deployed runtime that actually runs agents — a FastAPI service (`/api/v1`) + a worker pool that drains a job queue, on Azure Container Apps | HTTP (`/api/v1/...`) or `mdk ... --target <env>` |

Other core concepts:

- **Agent** — a unit of behavior: a `prompt.md` (instructions) + `agent.yaml`
  (model, input/output JSON schemas, contexts, skills) + optional eval dataset.
- **Project** — a workspace holding `agents/`, shared `contexts/`, `project.yaml`,
  and a local `.mdk/` state dir (sqlite `local.db`, `config.yaml` with your
  registered targets). *(Legacy projects use `.movate/`; both are resolved.)*
- **Target** — a named runtime you talk to: a deployed Azure env (`dev`, `prod`)
  or a local `mdk serve`. Registered with `mdk config add-target`.
- **Tenant** — the isolation boundary. Keys, runs, provider keys, budgets, and
  usage are all scoped per `tenant_id`; the storage layer enforces it.
- **Env vars** — canonical prefix is `MDK_*`; `MOVATE_*` is a transitional alias
  (both work — set either).

The inner dev loop runs **locally** (no Azure). The outer loop **deploys** to a
target and tests against the live runtime. You'll use both.

---

## 1. Authentication & identity

This is the part to internalize. There are **three distinct credential types** —
don't conflate them:

| # | Credential | What it authenticates | Primary commands |
| --- | --- | --- | --- |
| 1 | **Provider key** | mdk → the LLM (OpenAI/Anthropic) | `mdk auth login`, `mdk auth status`, `mdk keys` (per-tenant BYOK) |
| 2 | **Runtime bearer key** (`mvt_…`) | a client → the movate runtime API | `mdk auth create-key / rotate-key / revoke-key / whoami` |
| 3 | **OIDC SSO token** | a *human* → an OIDC-configured target | (device-code, automatic) + `mdk auth logout / whoami` |

### 1.1 Provider keys (the LLM credential)

The runtime needs an OpenAI/Anthropic key to call models.

```bash
mdk auth login            # prompts for a provider key, verifies it, persists it
mdk auth status           # shows which provider keys are set + where they came from
```

- **Machine-global**, reused across all your projects.
- **Per-tenant BYOK** (so each tenant brings its own provider key instead of a
  shared one): `mdk keys` (set / list / delete / test — see `mdk keys --help`)
  and the `/api/v1/provider-keys` API. Stored **Fernet-encrypted at rest**
  (decryptable only with the runtime's `MOVATE_PROVIDER_KEY_SECRET`); the
  plaintext value is never returned. The resolver uses the tenant's key when
  present and falls back to the shared key otherwise.

### 1.2 Runtime bearer keys (the API credential)

Opaque keys of the form `mvt_<env>_<tenant8>_<keyid>_<secret>` — the bearer
token for every call to the runtime API.

**How you get one** (an `admin`-scoped caller mints it):

```bash
mdk auth create-key --tenant-id <uuid> --env live --scope read,run --label ci-bot
# → the FULL key is printed to STDOUT exactly once (irrecoverable after).
#   Warnings go to STDERR, so `KEY=$(mdk auth create-key … --quiet)` is clean.
```

- **Scopes follow least-privilege.** Omitting `--scope` grants `read,run,eval`
  (NOT admin). Ask for more only when needed:

  | Scope | Grants |
  | --- | --- |
  | `read` | list/get agents, runs, jobs |
  | `run` | execute agents/workflows |
  | `eval` | run evals |
  | `kb:write` | ingest/modify knowledge bases |
  | `admin` | manage keys + create/update agents (per tenant) |
  | `fleet-admin` | everything, all tenants (expands to the full set) |

**Where it's stored.** You save the printed key yourself (password manager), or
let mdk store it locally:

```bash
mdk auth save-runtime-key …       # save a minted key into the local store
mdk auth refresh-runtime-key …    # mint + save a fresh one in one step
```

- Local credential store: a machine-global **credentials file** (`~/.movate/credentials`,
  perms-locked) **or** the **OS keychain**. Migrate between them:
  `mdk auth use-keychain` (file → keychain) / `mdk auth use-file` (back).
  `mdk auth status` tells you what's configured and from where.
- On a **deployed** runtime, the first admin key is seeded from Key Vault on pod
  startup. Operators run `mdk auth bootstrap-seed <target>` once (mints → uploads
  to KV → saves locally); `mdk auth pull-runtime-key` pulls it down on another
  machine.

**Use it** — any of:

```bash
curl -H "Authorization: Bearer $KEY" $API/api/v1/agents          # raw HTTP
mdk run my-agent "hello" --target dev                            # CLI reads the saved key for the target
mdk auth whoami --target dev                                     # confirm identity + scopes of the active key
```

**Refresh & revoke** (runtime keys are long-lived until you rotate/revoke):

```bash
mdk auth rotate-key  <key-id> --target dev   # zero-downtime: old + new valid during a grace window, then old expires
mdk auth revoke-key  <key-id> --target dev   # idempotent
mdk auth revoke-all  --tenant-id <uuid> --target dev   # compromise response: kill every active key for a tenant
mdk auth list-keys   --target dev            # audit what's outstanding (newest first)
```

### 1.3 OIDC SSO (human users)

For interactive users, a target can be configured to authenticate against your
**IdP** instead of a static key (`TargetConfig auth: oidc`). When you first hit
such a target, mdk runs a **device-code flow** (opens a browser code page),
caches the token, and **auto-refreshes** it — including transparent re-auth when
a call comes back `401` (so a mid-session expiry doesn't fail your command).

```bash
mdk auth whoami --target <oidc-target>   # who am I on this target
mdk auth logout --target <oidc-target>   # clear the cached SSO token
```

There is **no `mdk login`** command — OIDC login is automatic on first use of an
OIDC target; provider-key login is `mdk auth login`.

### 1.4 Usage tracking & attribution

Every run is attributable and observable:

- Each run persists a **RunRecord** with the **actor** (the `api_key_id` or OIDC
  subject), the **tenant_id**, full **metrics** (`cost_usd`, input/output tokens,
  provider, model), and a **`trace_id`**. So "who ran what, what it cost" is
  answerable per key/user and per tenant.
- **Cost reporting:** `mdk costs report` — spend per agent / provider from
  recorded runs.
- **Fleet view:** `mdk fleet` — read-only across tenants/keys/usage.
- **Budgets:** per-tenant monthly cost ceilings; requests over the limit get
  `429` (the runtime-edge rate-limiter is per-(tenant, scope)).
- **Control-plane audit:** key mint/revoke/rotate and canary promote/rollback
  emit structured audit events → Azure Log Analytics + an App Insights span
  ("who did what, when"). `mdk audit` surfaces them.
- **Distributed tracing:** every run's `trace_id` lands in App Insights as one
  trace (submit → queue → execute → result); logs carry the `trace_id` so you
  can pivot log↔trace. Inspect with `mdk trace`, `mdk logs`, `mdk monitor`.

---

## 2. The build → deploy → iterate workflow

### Prerequisites

```bash
mdk --version            # confirm install
mdk auth login           # set a provider key (for real LLM calls; skip if using --mock)
az login                 # only needed for deploying to Azure
```

---

### Step 1 — Initialize a project & add agents

**1a. Create the project workspace**

```bash
mdk init --project my-project      # scaffolds project.yaml + .gitignore + agents/ + initial snapshot
cd my-project
```

**1b. Add a *default* (built-in template) agent**

```bash
mdk templates list                 # browse the built-in templates
mdk init faq-bot --template faq    # scaffold one agent from a template
#   templates: default · faq · summarizer · classifier · chatbot · extractor
```

This creates `agents/faq-bot/` with `prompt.md`, `agent.yaml`, and input/output
schemas you can edit.

**1c. Add an agent *from the catalog*** (richer, role-ready agents)

```bash
mdk add --list                     # browse the catalog
mdk add --search warranty          # find by keyword
mdk add case-reasoner --preview    # inspect before adding
mdk add case-reasoner --name rma-reasoner   # add it (optionally rename)
```

Confirm what you have:

```bash
mdk validate                       # schema + prompt-lint the whole project
mdk show rma-reasoner              # inspect a resolved agent
```

> **Fast inner loop:** `mdk dev <agent>` is the guided front door —
> scaffold → edit → **live-test on every save** → deploy, all in one session.
> Prefer it while authoring; the steps below are the explicit equivalents.

---

### Step 2 — Deploy the agent

**2a. Register the target once** (a deployed Azure env):

```bash
mdk config add-target dev \
  --azure-subscription <sub-id> \
  --azure-resource-group <rg> \
  --azure-acr <acr-name> \
  --azure-env <container-apps-env>
```

**2b. Deploy** (builds the runtime image, pushes to ACR, rolls out api+worker):

```bash
mdk deploy --target dev            # build + push + update both apps + verify /healthz
# variants:
mdk deploy --target dev --dry-run         # plan only
mdk deploy --target dev --only worker     # roll just the worker
mdk deploy --target dev --skip-build --image-tag movate:<sha>   # redeploy/rollback an existing image
```

> **No Azure?** Run a local runtime instead: `mdk serve` (API) + `mdk worker`
> (queue drainer) in separate shells, then use `--target` pointed at localhost.

Verify it's live:

```bash
curl -s -o /dev/null -w "%{http_code}\n" $API/healthz   # expect 200
mdk doctor --target dev                                 # deploy-readiness checks (KV secrets, collector, worker health)
```

---

### Step 3 — Test inference against the agent

**Locally** (fast, no deploy):

```bash
mdk run ./agents/faq-bot "What is mdk?"          # plain string auto-wraps to the single required field
mdk run ./agents/faq-bot "hi" --stream            # token-by-token preview
mdk run ./agents/faq-bot "hi" --mock              # deterministic, no API key / no cost
```

**Against the deployed runtime:**

```bash
mdk run faq-bot "What is mdk?" --target dev        # CLI path (uses your saved key for 'dev')
```

…or hit the API directly:

```bash
API=https://<your-api-fqdn>
KEY=$(mdk auth create-key --tenant-id <uuid> --env live --scope read,run --quiet)   # or reuse a saved one

# synchronous (blocks for the result):
curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"input":{"question":"What is mdk?"}}' \
  "$API/api/v1/agents/faq-bot/runs?wait=true" | jq '{status, cost:.metrics.cost_usd, trace_id:.metrics.trace_id, output}'

# asynchronous (queue → worker drains): returns {job_id, status:"queued"}, then poll:
JOB=$(curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"input":{"question":"What is mdk?"}}' "$API/api/v1/agents/faq-bot/runs" | jq -r .job_id)
curl -s -H "Authorization: Bearer $KEY" "$API/api/v1/jobs/$JOB" | jq .status
```

The response carries `status`, `metrics` (cost, tokens, `trace_id`), and the
structured `output`. Note the `trace_id` — you'll use it in Step 6 to see the run
in App Insights.

---

### Step 4 — Update instructions and/or add contexts

Two ways to change behavior:

**4a. Edit the instructions** — open `agents/faq-bot/prompt.md` (and `agent.yaml`
for model/schema changes) and edit. Then:

```bash
mdk validate                       # catch schema/prompt issues before deploy
```

**4b. Add a context** (shared reference text injected into the prompt — e.g. a
tone guide, policy, glossary):

```bash
mdk contexts create house-style --agent faq-bot   # creates contexts/house-style.md AND wires it into the agent's contexts:
#   (edit contexts/house-style.md with the guidance you want injected)

# or wire an existing context in / out:
mdk contexts attach house-style --agent faq-bot
mdk contexts detach house-style --agent faq-bot
mdk contexts list                                  # see all contexts + which agents use them
```

> **See changes instantly without deploying:** `mdk dev faq-bot` re-runs the
> agent on every save of the prompt or a context, printing the new output (and a
> diff vs. the previous one) — the tightest "did my edit change the behavior?"
> loop. `mdk run ./agents/faq-bot "…"` does a one-shot local check.

---

### Step 5 — Update / redeploy

Once the local behavior is what you want, push it to the target:

```bash
mdk validate                       # final gate
mdk deploy --target dev            # rebuild image + roll new revisions (api + worker)
```

The registry is **versioned** — each publish is a new agent version. You can
review and revert:

```bash
mdk agent history faq-bot --target dev
mdk agent revert  faq-bot --target dev --to <version>
```

---

### Step 6 — Retest & observe the updated behavior

Re-run the **same input** you used in Step 3 and compare:

```bash
mdk run faq-bot "What is mdk?" --target dev        # observe the new answer/tone
```

You should see the change driven by your edited instructions and/or the attached
context (e.g. a more on-brand tone from `house-style.md`). To confirm
end-to-end:

```bash
mdk run faq-bot "What is mdk?" --target dev | jq '{trace_id:.metrics.trace_id, output}'
# then in Azure Portal → Application Insights (movate-…-appi) → Transaction search,
# paste the trace_id to see the full span tree (request → agent.execute → provider call),
# or query the Log Analytics workspace: union AppDependencies, AppRequests | where OperationId == "<trace_id>"
```

That closes the loop: **edit → validate → redeploy → retest → observe** — and
every iteration is fully traced and cost-attributed.

---

## 3. Day-2: evaluate, observe, operate

| Goal | Command |
| --- | --- |
| Quality eval against a dataset | `mdk eval <agent> --target dev` |
| A/B a new version safely | `mdk canary …` (weighted champion/challenger + assisted promote) |
| Cost report | `mdk costs report` |
| Live trace / logs / metrics | `mdk trace …`, `mdk logs --target dev`, `mdk monitor` |
| Job lifecycle | `mdk jobs list --target dev`, `mdk jobs cancel <id> --target dev` |
| Bulk/scheduled runs | `mdk batch …`, `mdk schedule …`, `mdk trigger …` |
| DR backup (control-plane state) | `mdk export state backup.json` / `mdk import state backup.json` |
| Deploy-readiness diagnostics | `mdk doctor --target dev` |

### Accessing & interpreting traces on Azure

The deployed runtime emits **generic OpenTelemetry** (no Azure SDK in the app);
an in-cluster **OTel Collector** forwards it to **Application Insights**. Because
the component is **workspace-based**, the same data is queryable two ways:

| Where | What you get |
| --- | --- |
| **App Insights** component `movate-<env>-appi` (Portal) | visual transaction search, app map, performance, failures |
| **Log Analytics** workspace `movate-<env>-logs` (Portal → Logs, KQL) | raw query over the `App*` tables |

**The key mapping:** a run's OTel **`trace_id`** (returned in `metrics.trace_id`)
is the App Insights **`OperationId`**. Grab it from any run and pivot on it.

**Portal (point-and-click):** Azure Portal → your App Insights `movate-<env>-appi`:
- **Transaction search** → paste the `trace_id` into the search → open the result
  for the **end-to-end transaction** (the span waterfall).
- **Application map** → service topology + dependency health/latency at a glance.
- **Performance** (latency percentiles per operation) and **Failures** (error
  breakdown) for aggregate views.

**KQL (Log Analytics → Logs, or App Insights → Logs):**

```kql
// 1. The full span tree for one run (paste the trace_id):
union AppRequests, AppDependencies, AppTraces, AppExceptions
| where OperationId == "<trace_id>"
| project TimeGenerated, Type, Name, DurationMs, Success
| order by TimeGenerated asc

// 2. Recent agent executions (latency + success), last hour:
AppDependencies
| where TimeGenerated > ago(1h) and Name == "agent.execute" and AppRoleName == "movate-runtime"
| summarize runs=count(), p95_ms=percentile(DurationMs,95), failures=countif(Success==false) by bin(TimeGenerated, 5m)

// 3. Cost / token usage from the runtime metrics:
AppMetrics
| where TimeGenerated > ago(24h) and Name startswith "mdk."
| summarize total=sum(Sum) by Name        // mdk.run.cost_usd, mdk.run.tokens, mdk.jobs.completed, …

// 4. Logs correlated to a trace (logs carry trace_id):
union AppTraces, ContainerAppConsoleLogs_CL
| where * has "<trace_id>"
| order by TimeGenerated asc
```

**How to read a trace:** one `OperationId` groups the whole run. You'll see the
**`agent.execute`** span (role `movate-runtime`) as the unit of work — its
`DurationMs` is the end-to-end run time and `Success` its outcome; nested
dependency spans are the provider/tool calls. Per-run **cost + tokens** ride on
the run record and the `mdk.run.*` metrics (table #3). The golden-signal **alert
rules** (`movate-<env>-appi-*`: dead-letter spike, error rate, p95 latency,
availability) evaluate over this same data — check them under App Insights →
Alerts.

> Find the current env's exact names with
> `az resource list -g <rg> --query "[?contains(name,'appi')||contains(name,'logs')].name"`,
> or just use `mdk trace <trace_id> --target <env>` / `mdk logs --target <env>`
> from the CLI.

---

## 4. Command cheat-sheet

```text
# Project / authoring
mdk init --project <name>          bootstrap a workspace
mdk init <agent> -t <template>     scaffold a default-template agent
mdk add <catalog-ref> --name <n>   add a catalog agent   (mdk add --list / --search)
mdk dev <agent>                    guided scaffold→edit→live-test→deploy
mdk contexts create <n> --agent <a>  add + wire a context
mdk validate                       schema + prompt lint
mdk run <path|name> "<input>"      run local (path) or deployed (--target), +--mock +--stream

# Deploy / operate
mdk config add-target <name> --azure-…   register a target
mdk deploy --target <env>          build + roll out
mdk doctor --target <env>          deploy-readiness checks
mdk eval <agent> --target <env>    quality gate

# Auth
mdk auth login                     set a provider (LLM) key
mdk auth create-key --tenant-id <id> --scope read,run   mint a runtime bearer
mdk auth whoami / list-keys / rotate-key / revoke-key / revoke-all
mdk auth use-keychain | use-file   where local creds live
mdk keys …                         per-tenant BYOK provider keys
```

---

## 5. Troubleshooting

- **`401 Unauthorized`** — bad/expired bearer. Check `mdk auth whoami --target <env>`;
  mint/save a fresh key (`mdk auth refresh-runtime-key`). OIDC targets re-auth
  automatically; if stuck, `mdk auth logout` then retry.
- **`403 missing required scope`** — your key lacks the scope (e.g. `run`/`admin`).
  Mint one with the right `--scope`.
- **Deploy fails preflight / runtime unhealthy** — `mdk doctor --target <env>`
  checks KV secrets, the OTel collector, and worker health and tells you what's
  missing.
- **Run returns `error` with a schema message** — the model output didn't match
  the agent's output schema (common with `--mock` against strict-schema agents).
  Use a real run, or loosen the schema.
- **Anything unexpected** — `mdk explain <run-id>` and `mdk trace <trace_id>`
  reconstruct what happened step by step.

---

*This workflow is intentionally the same locally and against a deployed target —
the only difference is `--target`. Author and test fast locally with `mdk dev` /
`mdk run`, then `mdk deploy` and retest against the live runtime with the exact
same commands.*
