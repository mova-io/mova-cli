# Serving-and-keys runbook — batch, streaming, polling, auth

Operating the runtime surface a deployed movate exposes: bulk async inference,
SSE streaming, job/run polling, and the API-key auth model (scopes, rotation,
bulk revoke, rate limits). Endpoints are verified against
`src/movate/runtime/app.py`; CLI flags against `src/movate/cli/*.py`.

Most CLI commands here talk to a **deployed runtime** and resolve a target the
same way: per-command `--target` / `-t` > top-level `-t` / `MOVATE_TARGET` >
the active target from `~/.movate/config.yaml`. Register one with
`mdk config add-target <name> --url <url> --key-env <ENV_VAR>` and the bearer is
read from that env var.

---

## 1. Batch — bulk async inference over a dataset

`mdk batch submit <agent> <dataset.jsonl>` POSTs a JSONL dataset; the runtime
enqueues **one ordinary `JobKind.AGENT` job per row**, all sharing a `batch_id`.
Because each row is a normal queue job, it inherits retry / dead-letter / canary
/ observability for free — there's no new execution path. A parent
`BatchRecord` lets `status` aggregate progress.

```bash
# Fire-and-forget against the active target
mdk batch submit faq-agent prompts.jsonl
# → {"batch_id": "...", "total": 42, "status": "queued"}

# Wait for the whole batch to finish
mdk batch submit faq-agent prompts.jsonl --wait

mdk batch status <batch_id>     # per-status aggregate + derived state
mdk batch list                  # this tenant's recent batches
```

`submit` options (from `src/movate/cli/batch_cmd.py`):

| flag | notes |
|---|---|
| `DATASET.JSONL` (arg) | one JSON object per line = one run's input. `-` reads stdin. Every non-empty line must be a JSON object (validated client-side; a bad line names its line number and exits 2 before anything leaves the machine). |
| `--target` / `-t` | deployment target. Omit for the active target. |
| `--wait` / `-w` | block until every row reaches a terminal state, then print the aggregate. |
| `--timeout` | max seconds to wait with `--wait` (default 600). After this the batch continues server-side; the CLI exits **124**. |
| `--poll-interval` | seconds between status polls (`--wait` only; default 2). |
| `--notify-email` | the worker notifies this address as **each row** reaches a terminal status. |
| `--output` / `-o` | `table` (default) or `json`. |

`status` shows the per-state counts: `queued`, `running`, `success`, `error`,
`safety_blocked`, `dead_letter`, plus a derived `state` (`complete` vs `…`). With
`--wait`, the CLI exits **1** if any row ended `error` / `safety_blocked` /
`dead_letter`, so CI can branch on it.

---

## 2. SSE streaming — render tokens live

`mdk run <agent> "<input>" --target <t> --stream` renders model tokens to stderr
as they arrive. It POSTs to `POST /api/v1/agents/{name}/runs/stream` (scope
**`run`**), which returns a `text/event-stream`.

```bash
mdk run faq "hello world" --target dev --stream
```

Event frames (`\n\n`-terminated):

| event | data | meaning |
|---|---|---|
| `token` | `{"text": "<delta>"}` | zero or more; concatenating every `text` reconstructs the model's raw output. |
| `done` | `{"run_id", "status", "metrics", "output"}` | terminal success. |
| `error` | `{"message", "code"}` | emitted instead of `done` on failure. |

Streaming is **additive observation** — same Executor stack, same bundle
resolution (incl. canary routing), same persistence as a one-shot run. The
streamed run writes its `RunRecord` exactly like a non-streamed run, so
`GET /api/v1/runs/{run_id}` works after the stream closes. The response sets
`Cache-Control: no-cache` and `X-Accel-Buffering: no` to defeat intermediary
buffering (matters behind nginx / Azure Front Door).

Errors from the stream endpoint: **401** (bad bearer), **403** (token lacks
`run`), **404** (agent not registered), **422** (bad body). Local `--stream`
(no `--target`) renders tokens from the local Executor; `--mock` has no real
token stream so streaming is silently skipped. Workflow + `--replay` modes
ignore `--stream`.

---

## 3. Job / run polling

An async submit (`mdk submit`, `POST /api/v1/agents/{name}/runs`, a triggered or
scheduled job) returns a `job_id`. Poll it, then fetch the run for the actual
output.

| what | versioned | unversioned (back-compat) | scope |
|---|---|---|---|
| poll a job | `GET /api/v1/jobs/{job_id}` | `GET /jobs/{job_id}` | `read` |
| list jobs | `GET /api/v1/jobs` (superset; supports `?agent=`, `?status=`, `?limit=`) | `GET /jobs` | `read` |
| fetch a run (incl. `output`) | `GET /api/v1/runs/{run_id}` | `GET /runs/{run_id}` | `read` |

> **Gotcha (live deploy):** a caller that submits via
> `POST /api/v1/agents/{name}/runs` naturally polls the **versioned**
> `GET /api/v1/jobs/{job_id}`. Those poll/fetch routes originally existed only
> *unversioned* and the obvious `/api/v1/...` path 404'd. Both the versioned and
> unversioned paths now work — the `/api/v1` ones are thin aliases delegating to
> the same handler (same `read` scope, same `JobView`/`RunView`, same
> tenant-scoping). `JobView` carries only `result_run_id`; fetch the run to see
> what the agent produced.

Tenant-scoping: a cross-tenant `GET /jobs/{id}` (or run) returns **404**, never
403 — the runtime never leaks that an id exists. From the CLI:

```bash
mdk jobs wait <job_id> --target dev   # poll to terminal
mdk jobs list --target dev
```

---

## 4. Auth — keys, scopes, rotation, revoke

movate keys are **opaque tokens** (`mvt_<env>_<tenantprefix>_<key_id>_<secret>`),
looked up by `key_id` with a constant-time `secret_hash` compare — **not** JWTs
(there is no shared signing secret). Each key maps to a tenant; runs / feedback /
registry are tenant-scoped.

### Scopes (ADR 013, least privilege)

Defined in `src/movate/core/auth.py`. Each scope is checked independently (no
hierarchy):

| scope | grants (examples) |
|---|---|
| `read` | GET list/detail — catalog, runs, evals, jobs, models, pricing, canary status/compare, workflow runs. |
| `run` | submit an agent run (`POST /run`, `POST /agents/{name}/runs`, `…/runs/stream`); post run feedback; **signal a paused workflow run**. |
| `eval` | kick off evals / benchmarks. |
| `kb:write` | KB write ops — ingest / clear / reindex a corpus. |
| `admin` | tenant administration — create/update/delete agents, manage the tenant's API keys (rotate / revoke / revoke-all), upload datasets, set/promote/rollback/delete a canary, create/delete triggers. |
| `fleet-admin` | cross-tenant / fleet administration. Historically the *only* scope value; an `admin`-superset (expands to all scopes). |

Back-compat (ADR 013): a key with **no explicit scopes** resolves to
`read,run,eval` at read time — so legacy keys keep working but get **403** on
`admin` endpoints (no legacy key silently gains admin). A legacy single
`scope == "fleet-admin"` expands to all scopes. `fleet-admin` passing every
scope check is orthogonal to *data* filtering — it still only reads its own
tenant's rows on tenant-scoped queries.

### Mint a key

```bash
# Interactive — full key on stdout once; "save now" warning on stderr
mdk auth create-key --tenant-id <uuid> --env live --label ci-bot

# An admin key (manage other keys + create agents)
mdk auth create-key --tenant-id <uuid> --scope admin,read

# Scripting — bare key on stdout
KEY=$(mdk auth create-key --tenant-id <uuid> --env live --quiet)
```

`--scope` is repeatable and/or comma-listed (`--scope read,run` or
`--scope read --scope run`). Valid values: `read, run, eval, kb:write, admin,
fleet-admin`. **Omitting `--scope` defaults to `read,run,eval`** (matching a
legacy key) — NOT admin. An unknown scope is a hard error. `--env` is `live`
(default) or `test`. The full key is printed **once** and is irrecoverable.

### Rotate (zero-downtime grace window)

```bash
NEW=$(mdk auth rotate-key <key_id> --yes)               # local store
mdk auth rotate-key <key_id> --grace 7d --target dev    # deployed runtime
```

Mints a successor (inheriting the old key's env/scopes/label — never
widens/narrows access) and keeps the **old** key valid for `--grace` (default
24h; `0` = immediate cutover; capped at 30d server-side). Both keys
authenticate during the window so in-flight clients can pick up the new key with
no downtime; after it, only the successor works. `--ttl-days` sets the
successor's validity (default 90; `0` = no expiry). With `--target` it calls
`POST /api/v1/auth/keys/{key_id}/rotate` (scope `admin`); the new key prints to
stdout once, the old key's grace-expiry to stderr.

### Bulk revoke (compromise response)

```bash
# Local — requires --tenant-id; spare one key so you aren't locked out
mdk auth revoke-all --tenant-id <uuid> --except <keep-this>

# Deployed — auto-spares your CALLING key by default
mdk auth revoke-all --target dev
```

Destructive — prompts for confirmation (`-y` to skip). Without `--target`,
operates on local storage and requires `--tenant-id`. With `--target`, calls
`POST /api/v1/auth/keys/revoke-all` (scope `admin`) and the runtime auto-spares
your calling key so a remote bulk-revoke can't lock you out; `--except` overrides
which key is spared.

### Manage / inspect

```bash
mdk auth list-keys                       # local storage
mdk auth list-keys --target dev          # GET /api/v1/auth/keys (caller's tenant)
mdk auth list-keys --target dev --include-revoked
mdk auth revoke-key <key_id>             # revoke a single key
mdk auth whoami --target dev             # resolve the calling identity + scopes
```

> **Fresh-deploy bootstrap key (gotcha).** On a fresh Azure deploy the runtime
> auto-seeds a bootstrap key from the `bootstrap-api-key` secret, populated by
> `mdk auth bootstrap-seed <target>` (a `fleet-admin` key) — *not* hand-set. See
> [`../azure-bootstrap.md`](../azure-bootstrap.md) steps 5/8.
>
> **"My saved key returns 401 after a deploy."** Auth is opaque-token lookup
> against the runtime's storage (no JWT signing secret to rotate). If a Container
> App revision recycles and the runtime falls back to a non-durable
> SQLite-in-pod store (e.g. `MOVATE_DB_URL` not honored), the key table vanishes
> with the pod and every minted key 401s. Recover in one step with
> `mdk auth refresh-runtime-key <target>` (mints + saves a fresh bearer via
> `az containerapp exec`), or manually `mdk auth save-runtime-key <target>
> <key>`. The durable fix is a real Postgres `MOVATE_DB_URL`.
>
> **Cold start (dev).** A dev runtime with `minReplicas = 0` scales to zero; the
> first request after idle pays a cold-start (and a one-time registry seed). Poll
> / retry rather than treating the first slow/failed call as an outage.

---

## 5. Rate limits

Two independent token-bucket ceilings (`src/movate/runtime/middleware.py`),
checked **after** auth succeeds. A request is allowed only if **both** allow;
either denies → **429** with `Retry-After` = the longer of the two waits.

| limiter | keyed by | configured by | default |
|---|---|---|---|
| **per-API-key** | `key_id` | `build_app(rate_limit_per_minute=...)` | 60/min |
| **per-tenant aggregate** | `tenant_id` (a ceiling across ALL of a tenant's keys, so minting more keys can't sidestep the cap) | `build_app(tenant_rate_limit_per_minute=...)` or env `MDK_TENANT_RATE_LIMIT_PER_MINUTE` | **off** (no-op) |

A value `<= 0` (or unset, for the tenant limiter) installs a no-op limiter that
always allows but still attaches headers. Every response carries:

| header | meaning |
|---|---|
| `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset` | per-key bucket (the canonical 429 contract). |
| `X-RateLimit-Tenant-Limit` / `X-RateLimit-Tenant-Remaining` / `X-RateLimit-Tenant-Reset` | per-tenant aggregate. With the default no-op tenant limiter these carry a sentinel `0` limit — present but inert. |

These headers are CORS-exposed so a browser client can read them and back off
proactively.

### Troubleshoot 429s

* Read `Retry-After` and wait that long — it already accounts for whichever
  ceiling is binding.
* Compare `X-RateLimit-Remaining` (per-key) vs `X-RateLimit-Tenant-Remaining`
  to see which limit you hit. If the tenant header is near zero, another key in
  the same tenant is consuming the shared ceiling — minting a new key won't help.
* To raise the per-tenant ceiling, set `MDK_TENANT_RATE_LIMIT_PER_MINUTE`
  (a non-integer value is ignored with a warning, leaving the limiter off).

---

## 6. Per-tenant provider keys (BYOK)

Each tenant can bring its own OpenAI/Anthropic (etc.) provider key
([ADR 018](../adr/018-tenant-provider-keys.md)) — per-tenant cost isolation and
no shared-key blast radius. Keys are **encrypted at rest** and resolved per-run,
with a shared-fleet fallback so existing single-key deployments keep working
unchanged.

* **CLI:** `mdk keys set <provider>` (prompts hidden; stores encrypted), `mdk keys
  list` (configured providers + masked fingerprint — **never the value**),
  `mdk keys delete <provider>`, `mdk keys test <provider>` (cheap live check).
* **API:** `PUT /api/v1/provider-keys/{provider}` (set; **`admin`** scope; the
  value is never returned — response carries `provider` + `fingerprint` only),
  `GET /api/v1/provider-keys` (list configured + fingerprints; `read`),
  `DELETE /api/v1/provider-keys/{provider}` (`admin`). All tenant-scoped.
* **Resolution order (per run):** the calling tenant's decrypted key → the shared
  fleet key **iff** `MOVATE_ALLOW_SHARED_PROVIDER_KEY` is set (**default on**, so
  a tenant with no key transparently uses the fleet `OPENAI_API_KEY` /
  `ANTHROPIC_API_KEY` — today's behavior) → with the flag **off**, a clear
  fail-closed error (strict isolation: a keyless tenant can't borrow the fleet key).
* **Encryption-at-rest key:** `MOVATE_PROVIDER_KEY_SECRET` (a Fernet data key;
  the `MDK_` alias is bridged). Lose it and tenants must re-enter their keys.
* **Runtime coverage:** wired for the default **litellm** runtime (the
  `api_key` flows into `litellm.acompletion`). The optional native
  `anthropic` / `openai` runtimes currently fall through to the env-default key
  (they'd need a provider-signature change) — documented in the executor.

Setup: set `MOVATE_PROVIDER_KEY_SECRET` (a `Fernet.generate_key()` value) on the
runtime, then per tenant `mdk keys set openai` / `mdk keys set anthropic`. Leave
the fleet `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` for the fallback, or unset
`MOVATE_ALLOW_SHARED_PROVIDER_KEY` to require every tenant to bring its own.

---

## See also

* [`orchestration.md`](orchestration.md) — schedules/triggers/HITL produce the
  jobs you poll here; the workflow-signal endpoint uses the `run` scope.
* [`improvement-loop.md`](improvement-loop.md) — canary endpoints use `admin`;
  eval/harvest use `eval`.
* [`../azure-bootstrap.md`](../azure-bootstrap.md) — deploy + first-key bootstrap.
* ADR 012 (`../adr/012-runtime-auth-resilience.md`), ADR 013
  (`../adr/013-end-to-end-identity.md`), ADR 018
  (`../adr/018-tenant-provider-keys.md`).
