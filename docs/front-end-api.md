# Front-end API audit — `/api/v1` runtime surface

> _Read-only audit of the MDK runtime (`src/movate/runtime/app.py`) for
> Mova iO front-end readiness. Maps the five conceptual front-end
> operations (init / add / validate / deploy / monitor) onto the actual
> `/api/v1` HTTP surface, enumerates every route + its required scope,
> and records what is **deliberately CLI/ops-only** (control plane) vs.
> what the runtime (execution plane) exposes over HTTP._
>
> Companion docs: [`angular-client.md`](angular-client.md) (TypeScript
> client generation + the contract narrative) and
> [`mova-io-mapping.md`](mova-io-mapping.md) (platform-box scorecard).
> A hermetic contract test (`tests/test_front_end_api_contract.py`)
> pins the key paths + scopes so a rename/removal fails CI — `/api/v1`
> is a documented compat contract (CLAUDE.md rule 5).

## How routes + scopes are wired

* All versioned routes hang off `v1 = APIRouter(prefix="/api/v1")`
  (`app.py:1743`), mounted with `app.include_router(v1)` (`app.py:6465`).
* A handful of pre-versioning routes stay **unversioned** for
  back-compat (`/healthz`, `/ready`, `/agents`, `/run`, `/jobs/*`,
  `/runs/*`, `/runs/*/feedback`). Several have `/api/v1` aliases (jobs,
  runs); others (`/run`, `/agents`) do not — the versioned equivalents
  are the resource-oriented `POST /api/v1/agents/{name}/runs` and
  `GET /api/v1/agents`.
* Each endpoint gates with `dependencies=[_scope(...)]`, where
  `_scope(*needed)` → `Depends(require_scope(auth_dep, *needed))`
  (`app.py:1326`, ADR 013 L2). **Flat scope model, AND semantics, no
  hierarchy** — an `admin` key does *not* implicitly satisfy `read`;
  each endpoint declares exactly the scope it needs.

### The scope vocabulary (`src/movate/core/auth.py`)

| Scope | Grants |
|---|---|
| `read` | GET list/detail (catalog, runs, evals, models, pricing, traces, KB read, …) |
| `run` | submit an agent run, cancel a job, thread messages, signal a workflow |
| `eval` | kick off evals / benchmarks, manage eval schedules, harvest datasets |
| `kb:write` | KB write ops — ingest / clear / reindex an agent corpus |
| `admin` | tenant administration — create/update/delete/publish agents, manage API keys, set provider keys, datasets, canary, triggers |
| `fleet-admin` | all-powerful: expands to the **full** scope set at check time (passes every scope check) |

A key with **no explicit scopes** resolves to the legacy default
`{read, run, eval}` (`effective_scopes`) — so a legacy key keeps working
on read/run/eval but gets `403` on `admin`/`kb:write` endpoints (ADR 013
D3, deliberate least privilege).

---

## API ↔ CLI mapping (the five conceptual operations)

For each front-end operation: the canonical `/api/v1` route(s), the
request/response Pydantic model, and the required scope.

### `init` — create an agent

The CLI `mdk init` is **two things**: (a) bootstrap a local project
(scaffolding on the operator's disk — *not* an HTTP op, see CLI/ops-only
below) and (b) author a single agent. The agent-authoring half maps to:

| CLI intent | Method + path | Request → Response | Scope |
|---|---|---|---|
| Create agent from the Mova iO "Onboard Agent" wizard (JSON) | `POST /api/v1/agents/from-wizard` | `WizardAgentSubmission` → `AgentCreatedView` | `admin` |
| Create agent from a pre-built bundle (multipart: individual canonical files **or** a `.zip`) | `POST /api/v1/agents` | multipart form → `AgentCreatedView` | `admin` |
| Create / replace a skill bundle | `POST /api/v1/skills` | multipart form → `SkillCreatedView` | `admin` |

**Readiness: ready for structured/wizard creation; NOT ready for the
`--llm` "describe it in English" flow.** See the
[`--llm` finding](#the---llm-finding-definitive) — neither endpoint
LLM-generates an agent from a free-text description.

### `add` — add an agent (to a project / catalog)

`mdk add <role>` scaffolds a role-templated agent into a *local*
project. There is no "add to catalog" verb distinct from create on the
runtime — the front-end equivalent of "make this agent exist on the
runtime" **is** the `init`-mapped create endpoints above. The catalog is
then read back via:

| CLI intent | Method + path | Request → Response | Scope |
|---|---|---|---|
| List the live agent catalog (marketplace metadata, facet filters) | `GET /api/v1/agents` | query `role/capabilities/tags` → `AgentCatalogView` | `read` |
| Fetch one agent's full spec + bundle metadata | `GET /api/v1/agents/{name}` | query `version` → `AgentDetailView` | `read` |
| Version history of one agent | `GET /api/v1/agents/{name}/versions` | → `AgentVersionsView` | `read` |
| Update an agent bundle in place | `PUT /api/v1/agents/{name}` | multipart (+ `If-Match`) → `AgentUpdatedView` | `admin` |
| Soft-delete an agent | `DELETE /api/v1/agents/{name}` | → delete view | `admin` |

**Readiness: ready.** Catalog/read + create/update/delete all exist.
Local role-template scaffolding (`mdk add --list-roles`) is intentionally
CLI/ops-only.

### `validate`

| CLI intent | Method + path | Request → Response | Scope |
|---|---|---|---|
| Run the prompt linter + cost forecast for a stored agent | `POST /api/v1/agents/{name}/validate` | → `AgentValidationView` | `read` |

Note: validation of a *not-yet-created* bundle happens implicitly — both
create endpoints (`POST /agents`, `POST /agents/from-wizard`) run the
same `load_agent()` Pydantic + linter + schema checks and reject with
`422` before anything persists. `POST /agents/{name}/validate` re-runs
the linter against an already-stored agent (read-scoped, non-mutating).

**Readiness: ready.** Note the asymmetry: there is **no
"validate-this-draft-bundle-without-persisting" endpoint** — a draft is
validated only as a side effect of the create call (which then 409s if
the name is taken). For a "lint before I commit" UX the front end either
relies on the 422 path or needs a new dry-run endpoint (see gap below).

### `deploy` — ship an agent

"Deploy" is overloaded. Two distinct senses:

| Sense | Method + path | Request → Response | Scope |
|---|---|---|---|
| Publish the agent bundle to GitHub as one commit (source-of-truth promotion, ADR 007) | `POST /api/v1/agents/{name}/publish` | `AgentPublishSubmission` → `AgentPublishedView` | `admin` |
| Promote/route prod traffic to a version (canary rollout) | `POST /api/v1/agents/{name}/canary` · `…/canary/promote` · `…/canary/rollback` | canary models → `CanaryView` | `admin` |
| Revert to a prior published version | `POST /api/v1/agents/{name}/revert` | → revert view | `admin` |

**Readiness: ready for agent-level promotion** (publish, canary,
promote, rollback, revert all exist). The **infra** sense of deploy —
building the runtime image and rolling Azure Container Apps
(`mdk deploy --mode runtime`) — is deliberately CLI/ops-only (bicep/az,
control plane). See CLI/ops-only below.

### `monitor` — runs / traces / evals / metrics

| CLI intent | Method + path | Request → Response | Scope |
|---|---|---|---|
| Submit an agent run (sync `wait=true` or async) | `POST /api/v1/agents/{name}/runs` | `AgentRunSubmission` → `RunAccepted \| RunView` | `run` |
| Stream tokens live (SSE) | `POST /api/v1/agents/{name}/runs/stream` | `AgentRunSubmission` → SSE | `run` |
| Poll a job to terminal | `GET /api/v1/jobs/{job_id}` | → `JobView` | `read` |
| List jobs (filter/paginate) | `GET /api/v1/jobs` | → job list | `read` |
| Fetch a finished run (+ output) | `GET /api/v1/runs/{run_id}` | → `RunView` | `read` |
| Reconstructed trace for the trace-viewer | `GET /api/v1/runs/{run_id}/trace` | → `RunTraceView` | `read` |
| Decision-chain explanation | `GET /api/v1/runs/{run_id}/explain` | → `RunExplainView` | `read` |
| Kick off an eval against the agent's dataset | `POST /api/v1/agents/{name}/evals` | `EvalSubmission` → `EvalAcceptedView` | `eval` |
| List eval history | `GET /api/v1/evals` | → `EvalListView` | `read` |
| Fetch an eval scorecard | `GET /api/v1/evals/{eval_id}` | → `EvalScorecardView` | `read` |
| Multi-model bench | `POST /api/v1/bench/{agent}` · `GET /api/v1/bench[/{id}]` | → `BenchResultView` | `eval` / `read` |
| Per-run operator feedback | `POST /runs/{run_id}/feedback` · `GET /runs/{run_id}/feedback` | unversioned | `run` / `read` |
| Model pricing / capabilities metrics | `GET /api/v1/models` · `GET /api/v1/pricing` | → catalog/pricing views | `read` |

**Readiness: ready.** The async run → poll job → fetch run → fetch
trace/explain → eval scorecard loop is fully exposed. (Aggregate
operational metrics — e.g. a dashboards JSON — are surfaced via the
Grafana/Prometheus/Azure dashboards-as-code path, not a `/api/v1` route.)

---

## Full `/api/v1` route inventory

75 versioned routes + 9 unversioned. Grouped; each row is method + path
+ one-line purpose + required scope. `-` = no `_scope()` gate (still
authenticated unless noted).

### Agents lifecycle

| Method | Path | Purpose | Scope |
|---|---|---|---|
| GET | `/api/v1/agents` | List the agent catalog (marketplace metadata + facet filters) | `read` |
| POST | `/api/v1/agents` | Create an agent from a multipart bundle (files or `.zip`) | `admin` |
| POST | `/api/v1/agents/from-wizard` | Create an agent from the Mova iO wizard JSON shape | `admin` |
| GET | `/api/v1/agents/{name}` | Full agent spec + bundle metadata | `read` |
| PUT | `/api/v1/agents/{name}` | Replace an agent bundle in place (supports `If-Match`) | `admin` |
| DELETE | `/api/v1/agents/{name}` | Soft-delete an agent (recovery window) | `admin` |
| POST | `/api/v1/agents/{name}/validate` | Prompt linter + cost forecast | `read` |
| GET | `/api/v1/agents/{name}/versions` | Durable-registry version history | `read` |
| GET | `/api/v1/agents/{name}/history` | GitHub commit history | `read` |
| POST | `/api/v1/agents/{name}/publish` | Push canonical bundle to GitHub (one commit) | `admin` |
| POST | `/api/v1/agents/{name}/revert` | Revert to a prior version | `admin` |
| POST | `/api/v1/skills` | Create / replace a skill bundle | `admin` |

### Execution (runs / threads / batches / jobs)

| Method | Path | Purpose | Scope |
|---|---|---|---|
| POST | `/api/v1/agents/{name}/runs` | Run an agent (sync `wait=true` or async) | `run` |
| POST | `/api/v1/agents/{name}/runs/stream` | Run + stream tokens over SSE | `run` |
| POST | `/api/v1/agents/{name}/batch` | Submit a dataset as a batch of async jobs | `run` |
| GET | `/api/v1/runs/{run_id}` | Fetch a single run incl. output | `read` |
| GET | `/api/v1/runs/{run_id}/trace` | Reconstructed run for the trace-viewer | `read` |
| GET | `/api/v1/runs/{run_id}/explain` | Decision-chain explanation | `read` |
| GET | `/api/v1/jobs` | Filterable + paginatable job history | `read` |
| GET | `/api/v1/jobs/{job_id}` | Job state (versioned alias of `/jobs/{id}`) | `read` |
| POST | `/api/v1/jobs/{job_id}/cancel` | Cooperatively cancel a queued/running job | `run` |
| GET | `/api/v1/batches` | List recent batches | `read` |
| GET | `/api/v1/batches/{batch_id}` | Aggregate status of a batch's child jobs | `read` |
| GET | `/api/v1/threads` | List multi-turn threads | `read` |
| POST | `/api/v1/threads` | Open a new conversation thread | `run` |
| GET | `/api/v1/threads/{thread_id}` | Get a thread (+ optional run history) | `read` |
| POST | `/api/v1/threads/{thread_id}/messages` | Submit a message in a thread | `run` |
| DELETE | `/api/v1/threads/{thread_id}` | Hard-delete a thread | `run` |
| GET | `/api/v1/workflow-runs` | List workflow runs | `read` |
| POST | `/api/v1/workflow-runs/{workflow_run_id}/signal` | Signal a human decision to resume a paused workflow | `run` |

### Eval / bench

| Method | Path | Purpose | Scope |
|---|---|---|---|
| POST | `/api/v1/agents/{name}/evals` | Run an eval against the agent's dataset | `eval` |
| GET | `/api/v1/evals` | Paginated eval history (filter by `agent`) | `read` |
| GET | `/api/v1/evals/{eval_id}` | Eval scorecard | `read` |
| POST | `/api/v1/bench/{agent}` | Kick off a multi-model bench | `eval` |
| GET | `/api/v1/bench` | Paginated bench history | `read` |
| GET | `/api/v1/bench/{bench_id}` | A bench's comparison | `read` |
| POST | `/api/v1/agents/{name}/dataset` | Upload / replace the agent's eval dataset | `admin` |
| POST | `/api/v1/agents/{name}/dataset/harvest` | Harvest prod runs into proposed eval cases | `eval` |
| PUT | `/api/v1/agents/{name}/eval-schedule` | Upsert continuous-eval cadence | `eval` |
| DELETE | `/api/v1/agents/{name}/eval-schedule` | Remove continuous-eval schedule | `eval` |
| GET | `/api/v1/eval-schedules` | List continuous-eval schedules | `read` |

### Knowledge base (per-agent)

| Method | Path | Purpose | Scope |
|---|---|---|---|
| GET | `/api/v1/agents/{name}/kb` | List KB chunks | `read` |
| POST | `/api/v1/agents/{name}/kb` | Ingest KB documents | `kb:write` |
| DELETE | `/api/v1/agents/{name}/kb` | Delete KB chunks | `kb:write` |
| POST | `/api/v1/agents/{name}/kb/reindex` | Rebuild the KB vector index | `kb:write` |
| POST | `/api/v1/agents/{name}/kb/search` | Semantic search over the KB | `read` |
| GET | `/api/v1/agents/{name}/kb/stats` | Aggregate KB stats | `read` |

### Canary / rollout

| Method | Path | Purpose | Scope |
|---|---|---|---|
| GET | `/api/v1/agents/{name}/canary` | Canary config / status | `read` |
| POST | `/api/v1/agents/{name}/canary` | Set / update canary rollout | `admin` |
| DELETE | `/api/v1/agents/{name}/canary` | Remove the canary (kill switch) | `admin` |
| GET | `/api/v1/agents/{name}/canary/compare` | Champion-vs-challenger live quality | `read` |
| POST | `/api/v1/agents/{name}/canary/promote` | Promote a version to champion | `admin` |
| POST | `/api/v1/agents/{name}/canary/rollback` | Roll back to the prior champion | `admin` |

### Monitor / metadata (models, pricing, provider keys, schedules, triggers)

| Method | Path | Purpose | Scope |
|---|---|---|---|
| GET | `/api/v1/models` | Catalog: pricing + capabilities | `read` |
| GET | `/api/v1/models/{model_id:path}` | Pricing + capabilities for one model | `read` |
| GET | `/api/v1/pricing` | Packaged model pricing table | `read` |
| GET | `/api/v1/provider-keys` | List configured provider keys (fingerprints) | `read` |
| PUT | `/api/v1/provider-keys/{provider}` | Set / rotate this tenant's BYOK provider key | `admin` |
| DELETE | `/api/v1/provider-keys/{provider}` | Remove this tenant's provider key | `admin` |
| GET | `/api/v1/schedules` | List cron schedules | `read` |
| GET | `/api/v1/schedules/{name}` | Fetch one cron schedule | `read` |
| PUT | `/api/v1/schedules/{name}` | Upsert a cron schedule | `run` |
| DELETE | `/api/v1/schedules/{name}` | Remove a cron schedule | `run` |
| GET | `/api/v1/triggers` | List event/webhook triggers (no secrets) | `read` |
| POST | `/api/v1/triggers` | Register an inbound event/webhook trigger | `admin` |
| GET | `/api/v1/triggers/{name}` | Fetch one trigger (no secret) | `read` |
| DELETE | `/api/v1/triggers/{name}` | Remove a trigger | `admin` |
| POST | `/api/v1/triggers/{trigger_id}/events` | Fire a trigger (external caller; HMAC-signed, **not** api-key auth) | `-` |

### Auth / keys

| Method | Path | Purpose | Scope |
|---|---|---|---|
| GET | `/api/v1/auth/me` | Identity of the calling bearer key | `-` (auth required) |
| GET | `/api/v1/auth/keys` | List the tenant's active keys | `admin` |
| POST | `/api/v1/auth/keys` | Mint a new scoped key (returned once) | `admin` |
| POST | `/api/v1/auth/keys/{key_id}/rotate` | Rotate a key (grace window) | `admin` |
| DELETE | `/api/v1/auth/keys/{key_id}` | Revoke a key | `admin` |
| POST | `/api/v1/auth/keys/revoke-all` | Revoke every active key for the tenant | `admin` |

### Meta

| Method | Path | Purpose | Scope |
|---|---|---|---|
| GET | `/api/v1/openapi.json` | Versioned alias of the OpenAPI spec | `-` (public) |

### Unversioned (pre-versioning, back-compat)

| Method | Path | Purpose | Scope |
|---|---|---|---|
| GET | `/healthz` | Liveness probe (never hits storage) | `-` |
| GET | `/ready` | Readiness probe (deep checks) | `-` |
| GET | `/agents` | List agents on this runtime | `read` |
| POST | `/run` | Queue a job for the worker | `run` |
| GET | `/jobs` | Recent jobs, newest first | `read` |
| GET | `/jobs/{job_id}` | Job state | `read` |
| GET | `/runs/{run_id}` | A single run incl. output | `read` |
| GET | `/runs/{run_id}/feedback` | List feedback for a run | `read` |
| POST | `/runs/{run_id}/feedback` | Create / update operator feedback | `run` |

---

## CLI / ops-only (deliberately NOT HTTP endpoints)

These are **control-plane** operations that run on the operator's
machine or a CI runner, not on the runtime (execution plane). The
control plane ⊥ execution plane boundary (CLAUDE.md rule 6) is why they
have no `/api/v1` route — the front end drives them through the operator
workflow / CI, not directly over the runtime API.

* **Local project scaffolding** — `mdk init` (project mode, outside a
  project) and `mdk add <role>` write `project.yaml` + `agents/<name>/`
  to the operator's working directory. This is filesystem authoring, not
  a runtime mutation. The runtime equivalent of "make this agent exist
  on the server" is `POST /api/v1/agents` / `…/from-wizard`.
* **Infra provisioning / runtime deploy** — `mdk deploy --mode runtime`
  shells out to `az acr build` + Azure Container Apps (bicep/az) to
  build and roll the runtime image. This provisions the *thing that
  serves* `/api/v1`; it cannot live behind `/api/v1`. (`mdk deploy
  --mode agents` *does* talk to the runtime — it just POSTs bundles to
  `POST /api/v1/agents`, i.e. the create endpoint above.)
* **LLM-assisted agent authoring** — `mdk init --llm "<description>"` /
  `mdk dev --llm` scaffold an agent from natural language using the
  `movate.authoring` planner stack. This is CLI-only (see below).
* **Local dev loop** — `mdk dev`, `mdk chat`, `mdk run` (local),
  `mdk plan`, `mdk eval-gen`, `mdk simulate`, etc. drive a local
  in-process runtime; the deployed runtime exposes the run/eval HTTP
  equivalents listed in the inventory.

---

## Auth — how the front end authenticates

* **Bearer key + scopes (ADR 013).** Every request (except `/healthz`,
  `/ready`, the public OpenAPI alias, and the HMAC-signed
  `POST /triggers/{id}/events`) carries
  `Authorization: Bearer <mvt_...>`. The middleware
  (`src/movate/runtime/middleware.py`) parses the bearer token, looks up
  the key record, charges the rate limiter, and resolves the key's
  **least-privilege scopes** (`effective_scopes`). Each endpoint's
  `require_scope(...)` then checks the declared scope(s) with AND
  semantics — a missing scope returns the standard `403 FORBIDDEN`
  envelope naming the required scope.
* **OIDC option.** When `MOVATE_OIDC_ISSUER` is set, a JWT-shaped bearer
  token is validated as an OIDC token instead (`src/movate/runtime/oidc.py`)
  — issuer/audience checked, tenant from `MOVATE_OIDC_TENANT_CLAIM`
  (default `tid`), scopes mapped from `MOVATE_OIDC_SCOPE_CLAIM`. This is
  the path for per-user SSO (e.g. Azure AD); the opaque-key path is the
  v0.7 default. Opaque `mvt_*` keys are never attempted as OIDC.
* **CORS.** Browser callers need `MDK_CORS_ALLOWED_ORIGINS`
  (comma-separated) set per environment — dev permissive (`*`), staging
  + prod pinned to the Mova iO web host. Bearer-token auth uses
  `allow_credentials=False` (tokens ride the `Authorization` header, not
  cookies), so `*` works in dev.

### Obtaining a scoped key

1. **Bootstrap (operator / CI, CLI):**
   `mdk auth create-key --tenant-id <uuid> --scope admin,read`
   mints the first management key (server-side, against the configured
   storage). Omitting `--scope` yields the legacy default
   `{read, run, eval}` — *not* admin.
2. **Self-service (front end, HTTP):** with an `admin` key, call
   `POST /api/v1/auth/keys` (body `ApiKeyMintRequest`: `label`,
   `ttl_days`, `scopes`) → `ApiKeyMintedView`. The `full_key` is shown
   **once** and is irrecoverable — store it immediately. Unknown scope
   strings 400/422; omitted scopes → legacy default.
3. The front end typically runs behind a **backend-for-frontend proxy**
   holding a fleet key in v0.7 alpha; per-user scoped keys land with the
   OIDC path.

`GET /api/v1/auth/me` (no scope gate, but auth required) returns the
calling key's `key_id`, tenant, env, scopes, and expiry — handy for the
front end to discover what it can do.

---

## The `--llm` finding (definitive)

**Question:** does any `/api/v1` endpoint accept a free-text natural-
language description and **LLM-generate** the agent (the CLI
`mdk init --llm` magic), or do the endpoints only accept **structured
fields / pre-built bundles**?

**Answer: NO endpoint does LLM generation. All agent-creation endpoints
are structured / pre-built-bundle only.** Evidence:

* **`POST /api/v1/agents/from-wizard`** (`app.py:1960`,
  `WizardAgentSubmission` at `schemas.py:1244`) takes **structured
  fields**: `name`, `agent_prompt` (the *actual prompt template* the
  wizard collects — required, `min_length=1`), `ai_model`, `description`,
  goals, persona, connectors, etc. The handler calls
  `wizard_to_bundle_files(body)` (`agent_creation.py:465`), which is a
  **pure dict → dict transform** — it slugifies the name, maps fields
  onto canonical `agent.yaml` keys, and writes `agent_prompt` into
  `prompt.md` **verbatim**. No model call, no expansion. Its own
  docstring flags LLM schema inference as a *future enhancement
  (item 93)* — i.e., explicitly not implemented.
* **`POST /api/v1/agents`** (`app.py:1819`) takes **multipart files** —
  either the four individual canonical files (`agent.yaml`, `prompt.md`,
  schemas) or a `.zip` bundle. Pre-built bytes land on disk as-is.
* A repo-wide search of `src/movate/runtime/` for LLM-generation /
  "natural language" / authoring-copilot hooks returns **nothing** — the
  `--llm` planner (`movate.authoring.planner`) is imported only by the
  CLI (`src/movate/cli/dev_cmd.py`, `…/init.py`), never by the runtime.

**Consequence:** if the Mova iO front end wants the "describe an agent in
plain English and have it generated" UX, that capability does **not**
exist over HTTP today. Two implementation paths (both require a new
endpoint / ADR — out of scope for this audit):
(a) a new `POST /api/v1/agents/draft` (or `from-description`) that runs
the authoring planner server-side, or
(b) the Mova iO BFF runs the CLI/planner itself and then POSTs the
resulting bundle to the existing `POST /api/v1/agents`.

---

## Biggest gap for the front end

**No server-side LLM authoring + no draft/dry-run create.** The runtime
exposes structured creation only (wizard JSON or pre-built bundles), so
two front-end UX expectations are unmet:

1. **"Describe it in English → generate"** — the `mdk init --llm` magic
   has no HTTP equivalent (see the `--llm` finding). The front end must
   either pre-render a bundle (via its own BFF/CLI) or wait for a new
   server-side authoring endpoint.
2. **"Lint my draft before I commit it"** — validation only runs as a
   side effect of the create call (which then conflicts on an existing
   name). There is no "validate this candidate bundle without persisting"
   endpoint; `POST /agents/{name}/validate` only works on an
   already-stored agent.

Both point at the same shape: a draft/preview create endpoint (generate
and/or validate without persisting) is the highest-value addition for a
smooth front-end authoring flow.
