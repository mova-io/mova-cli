# MDK API — endpoint smoke guide for Mova iO integration

> Audience: Deva + the Mova iO Angular team
> Time to complete: ~15 minutes
> Outcome: You have a working bearer, you've personally hit every
> endpoint the Mova iO front end will call, and you know what each
> response looks like.

This walks you through every endpoint the Mova iO front end will
integrate with against the MDK runtime. Each section follows the
same shape:

1. **What this endpoint is for** — one sentence
2. **The curl** — paste verbatim
3. **Expected response** — what you should see
4. **Angular notes** — common gotchas when wiring it into a typed client

Run the steps in order. Every curl uses `${MDK_BASE}` and `${MDK_TOKEN}`
shell variables — Section 0 sets them once, then everything else
runs paste-as-is.

---

## Section 0 — Setup (once per shell session)

### 0.1 — Find your connection info

The MDK team sends you four values out-of-band (Teams DM, email, or
your password manager). They look like:

```
Runtime URL:    https://movate-dev-api.<hash>.eastus2.azurecontainerapps.io
Bearer token:   mvt_live_<tenant>_<keyid>_<secret>
Tenant ID:      <uuid>
CORS allow:     http://localhost:4200 (your prod Mova iO origin gets
                added by the MDK team when you send us the hostname)
```

If you don't have them yet, ping the MDK team. They mint a bearer
for your tenant via a one-line internal command — takes ~30 seconds
for them to send you a fresh one. You should never need to generate
this yourself.

### 0.2 — Export them in your shell

Paste this with the real values from the onboarding message
substituted in:

```bash
export MDK_BASE="https://movate-dev-api.<hash>.eastus2.azurecontainerapps.io"
export MDK_TOKEN="mvt_live_<your-bearer>"
```

Verify both are set:

```bash
echo "${MDK_BASE}"
echo "${MDK_TOKEN}" | head -c 20 && echo "..."
```

### 0.3 — Treat the bearer like a password

- **Don't commit it** to git. If you use a `.env` file in your
  Angular project, gitignore it.
- **Don't paste it in tickets, chat, or screenshots.**
- **If it ever stops working** (every previously-good request
  starts returning 401): it's been revoked or rotated. Ping the
  MDK team — they'll mint you a new one in ~30 seconds.

### 0.4 — One helper you'll want — JSON pretty-printing

Every smoke command below pipes through `python3 -m json.tool` so
the response renders readably. Python 3 is pre-installed on macOS
and Linux. If you prefer `jq`, that works too — `brew install jq`
on macOS.

You're set. Run through Sections 1-8 in order.

---

## Section 1 — Liveness + readiness (unauthed)

### 1.1 — `GET /healthz`

**What it's for:** Cheap "is the runtime alive?" probe. No auth, no
DB access. Safe to ping on every page-load if you want, though once
on app boot is plenty.

```bash
curl -s "${MDK_BASE}/healthz" | python3 -m json.tool
```

**Expected:**
```json
{
    "status": "ok",
    "version": "0.7.0"
}
```

**Angular notes:**
- Status code is always 200 (unless the pod is fully dead — at which
  point you'd see a network error, not a non-200).
- Use this as a "tell me what version is deployed" probe so your
  client can warn if the runtime version drifts from what your
  client was generated against.

### 1.2 — `GET /ready`

**What it's for:** Deeper "can the runtime serve requests?" probe.
Pings the DB. Returns 503 if the DB is dead.

```bash
curl -s "${MDK_BASE}/ready" | python3 -m json.tool
```

**Expected:**
```json
{
    "status": "ready",
    "version": "0.7.0",
    "checks": {
        "storage": "ok"
    }
}
```

**Angular notes:**
- This is the endpoint Azure's container probe uses internally; you
  rarely need to call it from the Angular client, but it's useful
  on a status-page widget.

---

## Section 2 — Browse the agent catalog

### 2.1 — `GET /agents` (catalog list)

**What it's for:** List every agent the runtime knows about. Used to
populate your "browse agents" screen.

```bash
curl -s "${MDK_BASE}/agents" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected:**
```json
{
    "agents": [
        {
            "name": "faq-agent",
            "version": "0.1.0",
            "description": "An FAQ assistant. Answers questions concisely with a confidence score."
        },
        ...
    ]
}
```

**Angular notes:**
- Note the URL is `/agents`, NOT `/api/v1/agents`. The unversioned
  path is the catalog list endpoint; the v1 paths are for per-agent
  operations.
- Returns metadata only — no prompt content, no schemas. Use 2.2 to
  fetch the full agent profile.

### 2.2 — `GET /api/v1/agents/{name}` (agent detail)

**What it's for:** Full profile for one agent — schemas, prompt
metadata, dataset info, marketplace fields (role, persona, etc.).
This is what your "agent profile" screen renders.

```bash
curl -s "${MDK_BASE}/api/v1/agents/faq-agent" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool | head -60
```

**Expected:** A large JSON object including `name`, `version`,
`description`, `model`, `input_schema`, `output_schema`, `dataset`
(if the agent has eval cases), plus marketplace metadata like `role`,
`persona`, `capabilities`, `tags` when present.

**Angular notes:**
- The full response can be a few KB. Cache it client-side for the
  duration of a session.
- `dataset` is `null` for agents that haven't had an eval dataset
  uploaded yet (e.g. anything you created via the wizard).
- `files` lists the per-agent files on the runtime's filesystem —
  useful for a "files in this agent" panel.

---

## Section 3 — Create + edit an agent

### 3.1 — `POST /api/v1/agents/from-wizard` (Mova iO wizard path)

**What it's for:** **This is your primary create endpoint.** It
accepts the field shape from your "Onboard Agent" wizard and
translates it into a canonical agent bundle on the runtime.

```bash
curl -s -X POST "${MDK_BASE}/api/v1/agents/from-wizard" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{
        "name": "smoke-bot",
        "agent_provider": "Movate",
        "agent_type": "Task Agent",
        "role": "FAQ assistant",
        "description": "Smoke-test agent created by Deva",
        "agent_role": "You are a friendly assistant that answers user questions.",
        "agent_goal": "Resolve user questions in 1-2 sentences.",
        "agent_prompt": "Question: {{ input.input }}\n\nAnswer concisely.",
        "reference_output": "A clear, concise answer.",
        "mcp_connectors": [],
        "knowledge_store": "",
        "ai_model": "gpt-4o-mini",
        "ai_foundation": "azure"
    }' | python3 -m json.tool
```

**Expected:**
```json
{
    "name": "smoke-bot",
    "version": "0.1.0",
    "description": "Smoke-test agent created by Deva",
    "agent_dir": "smoke-bot",
    "files_persisted": [
        "agent.yaml",
        "prompt.md",
        "schema/input.json",
        "schema/output.json"
    ]
}
```

**Angular notes:**
- **Field name `agent_role` is the prompt body**, not the marketplace
  role label. The `role` field is the marketplace label (e.g. "FAQ
  assistant" — shown as a chip in the catalog).
- The runtime auto-generates default input + output schemas (one
  free-form `input` string in, one free-form `output` string out).
  Mova iO can override these later via the multipart `POST
  /api/v1/agents` path if you need typed schemas.
- The agent's `name` becomes a URL slug everywhere — keep it
  lowercase, hyphens only, no spaces.
- 201 on success, 422 if `name` collides with an existing agent,
  400 if the body fails validation.

### 3.2 — `POST /api/v1/agents/{name}/validate`

**What it's for:** Lint the agent's prompt + forecast eval cost. Run
this after every save to give the user immediate feedback in the UI.

```bash
curl -s -X POST "${MDK_BASE}/api/v1/agents/smoke-bot/validate" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected:**
```json
{
    "passed": true,
    "errors": [],
    "warnings": [],
    "cost_forecast": null
}
```

**Angular notes:**
- `errors` and `warnings` are arrays of `{code, severity, message,
  hint}` objects. If non-empty, render them inline next to the prompt
  field in the wizard.
- `cost_forecast` is `null` for agents without a dataset — it kicks
  in once you upload eval cases.

### 3.3 — `POST /api/v1/agents` (multipart — for typed schemas)

**What it's for:** Power-user alternative to the wizard endpoint.
Accepts either a zipped bundle OR four individual files (agent.yaml,
prompt.md, schema/input.json, schema/output.json) + optional eval
dataset. Use this when you want to push fully-typed I/O schemas
rather than the wizard's free-form defaults.

Skip this one for the first integration pass — the wizard endpoint
covers 95% of Mova iO's create flow. We can dig into the multipart
shape later if you need typed schemas.

---

## Section 4 — Run an agent

There are TWO ways to run an agent. Pick based on the agent type:

| Agent created via | Use this run mode |
|---|---|
| Wizard (Section 3.1) | **Inline mode** — `?wait=true` |
| Baked into the runtime image | Either mode works |

The reason is a multi-pod filesystem isolation in Azure Container
Apps. Wizard-created agents land on the API pod's disk and aren't
visible to the worker pod. `?wait=true` runs them on the API pod
directly. Internal item 109 in our backlog fixes this; until then,
follow the table.

### 4.1 — Inline run (`?wait=true`)

**What it's for:** Synchronous run — the API holds the connection
open, executes the agent, and returns the result in one round-trip.
Latency = LLM call latency (typically 2-5 seconds).

```bash
curl -s -X POST "${MDK_BASE}/api/v1/agents/smoke-bot/runs?wait=true" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"input": {"input": "What is Movate?"}}' \
    | python3 -m json.tool
```

**Expected:**
```json
{
    "run_id": "<uuid>",
    "job_id": "<uuid>",
    "agent": "smoke-bot",
    "agent_version": "0.1.0",
    "provider": "openai/gpt-4o-mini-2024-07-18",
    "status": "success",
    "input": {"input": "What is Movate?"},
    "output": {"output": "Movate is a technology services company..."},
    "metrics": {
        "latency_ms": 2023,
        "tokens": {"input": 31, "output": 14, "cached_input": 0},
        "cost_usd": 1.3e-05
    },
    "error": null
}
```

**Save the `run_id`** — you'll use it in Section 4.4 (trace).

**Angular notes:**
- HTTP 200 on success (NOT 202 like the async path).
- `metrics.cost_usd` is in dollars; format it as `$0.000013` for
  display.
- Worth wrapping your HTTP client with a 60s timeout — slow LLM
  responses can take 20-30 seconds for long outputs.
- The wrapper response is a "RunView" type — same shape as
  `GET /runs/{run_id}` returns.

### 4.2 — Async run (default — for baked-in agents only)

**What it's for:** Queue-based run. Returns 202 immediately, your
client polls `GET /jobs/{id}` until terminal. Useful when the user
might navigate away.

```bash
curl -s -X POST "${MDK_BASE}/api/v1/agents/faq-agent/runs" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"input": {"question": "what is movate?"}}' \
    | python3 -m json.tool
```

**Expected:**
```json
{
    "job_id": "<uuid>",
    "status": "queued"
}
```

**Save the `job_id`** for the polling step (4.3).

**Angular notes:**
- HTTP 202 on success.
- Don't poll faster than every 1 second; the underlying queue check
  is on a similar interval.
- **If you POST against a wizard-created agent without `?wait=true`,
  the job will land but the worker will fail with `unknown_agent` +
  a hint pointing you at this section.** That's intentional —
  surfaces the cross-pod issue clearly until item 109 lands.

### 4.3 — `GET /jobs/{id}` (poll job status)

**What it's for:** Poll a queued job until it's terminal. Used in
conjunction with the async run path.

```bash
JOB_ID="<paste-from-4.2>"
curl -s "${MDK_BASE}/jobs/${JOB_ID}" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected (terminal success):**
```json
{
    "job_id": "<uuid>",
    "kind": "agent",
    "target": "faq-agent",
    "status": "success",
    "input": {"question": "what is movate?"},
    "result_run_id": "<uuid>",
    "error": null,
    "created_at": "2026-05-14T15:12:57Z",
    "claimed_at": "2026-05-14T15:12:57Z",
    "completed_at": "2026-05-14T15:13:00Z"
}
```

**Status values:**
- `queued` — not yet picked up by a worker
- `running` — worker is executing now
- `success` — done; `result_run_id` populated
- `error` — failed; check `error.type`, `error.message`, `error.hint`
- `dead_letter` — exhausted retries on transient errors

**Angular notes:**
- Poll every 1-2 seconds. Stop polling on any terminal status.
- Once status is `success`, fetch the full RunView via 4.4 using
  `result_run_id`.
- The `error.hint` field, when present, points the user at the
  workaround — great for surfacing as a tooltip when an error
  card displays.

### 4.4 — `GET /runs/{run_id}` (full run output)

**What it's for:** Fetch the full RunView — output payload + metrics
— for a successful job.

```bash
RUN_ID="<paste-from-4.3-result_run_id>"
curl -s "${MDK_BASE}/runs/${RUN_ID}" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected:** Same shape as the inline-run response (Section 4.1).

**Angular notes:**
- This is what your "run result" panel renders.
- Cache by `run_id` — runs are immutable; once fetched, they don't
  need to be re-fetched.

### 4.5 — `GET /jobs?limit=N` (recent activity)

**What it's for:** Paginated list of recent jobs for your "activity
feed" UI.

```bash
curl -s "${MDK_BASE}/jobs?limit=10" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected:** An envelope `{jobs: [...], count: N}` where each item
matches the shape from 4.3.

**Angular notes:**
- Scoped to your tenant — you'll never see another tenant's jobs.
- Useful as the home-screen "what's been running" widget.

### 4.6 — `GET /api/v1/runs/{run_id}/trace` (timeline)

**What it's for:** Full timeline for one run — prompt, response,
provider metadata, every span. This powers a "trace viewer" UI
similar to Langfuse's trace tree.

```bash
RUN_ID="<paste-from-earlier>"
curl -s "${MDK_BASE}/api/v1/runs/${RUN_ID}/trace" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected:** A `{kind, run, workflow, nodes, total_cost_usd,
total_latency_ms}` object. For single-agent runs, `workflow` is null
and `nodes` is empty.

**Angular notes:**
- Same shape works for workflow runs (multi-node). Branching on
  `kind === "workflow"` is the cleanest split in your trace-viewer
  component.

---

## Section 5 — Evaluate an agent

### 5.1 — `POST /api/v1/agents/{name}/evals`

**What it's for:** Kick off a scored eval against the agent's
dataset. Runs each case through the agent, scores against expected
answers, returns aggregate metrics.

```bash
curl -s -X POST "${MDK_BASE}/api/v1/agents/faq-agent/evals" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"gate": 0.0, "runs": 1, "mock": true}' \
    | python3 -m json.tool
```

**Expected (when the agent has a dataset):**
```json
{
    "eval_id": "<uuid>",
    "status": "success",
    "message": ""
}
```

**Expected (when the agent has no dataset — e.g. fresh wizard agent):**
```json
{
    "eval_id": "",
    "status": "failed",
    "message": "agent smoke-bot has no evals.dataset configured"
}
```

**Angular notes:**
- `mock: true` runs with a deterministic mock provider — useful for
  CI / smoke without burning API budget. Use `mock: false` (or omit
  the field) for real LLM eval.
- `gate` is the pass threshold (0.0-1.0); below this triggers
  `status: failed` on the resulting scorecard.
- `runs` is the number of times to repeat each case for averaging
  (default 1; bump to 3-5 to smooth out LLM nondeterminism).
- Wizard-created agents will return `failed` until you upload a
  dataset. **A future endpoint** (item 111 in our backlog) will let
  you POST a dataset.jsonl after-the-fact; until then, datasets need
  to land via the multipart agent-create path.

### 5.2 — `GET /api/v1/evals/{eval_id}` (scorecard)

**What it's for:** Fetch the scorecard for one eval — aggregate
scores, dimensional breakdown, dataset metadata.

```bash
EVAL_ID="<paste-from-5.1>"
curl -s "${MDK_BASE}/api/v1/evals/${EVAL_ID}" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected:**
```json
{
    "eval_id": "<uuid>",
    "agent": "faq-agent",
    "agent_version": "0.1.0",
    "dataset_hash": "<sha256-prefix>",
    "judge_method": "exact",
    "judge_provider": null,
    "runs_per_case": 1,
    "gate_mode": "mean",
    "threshold": 0.7,
    "mean_score": 0.85,
    "pass_rate": 0.9,
    "sample_count": 10,
    "total_cost_usd": 0.0034,
    "created_at": "2026-05-14T15:22:28Z"
}
```

**Angular notes:**
- `judge_method` is `"exact"` (string match) or `"llm"` (a different-
  family LLM acts as judge).
- `mean_score` is the aggregate quality score; `pass_rate` is the
  fraction of cases above `threshold`. Render both.

### 5.3 — `GET /api/v1/evals?agent={name}` (eval history)

**What it's for:** List all evals for one agent. Powers the "eval
history" panel on the agent profile.

```bash
curl -s "${MDK_BASE}/api/v1/evals?agent=faq-agent" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected:** `{evals: [...], count: N}` envelope. Sorted newest-first.

**Angular notes:**
- Each row carries enough metadata to render a "trend" sparkline
  (mean_score over time) without follow-up calls.

---

## Section 6 — Delete (soft)

### 6.1 — `DELETE /api/v1/agents/{name}`

**What it's for:** Remove an agent from the catalog. Soft-delete —
the bundle moves to a sibling `.deleted-<name>-<timestamp>/`
directory; recoverable out-of-band by the ops team for ~7 days.

```bash
curl -s -X DELETE "${MDK_BASE}/api/v1/agents/smoke-bot" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool
```

**Expected:**
```json
{
    "name": "smoke-bot",
    "deleted_dir": ".deleted-smoke-bot-1778772217"
}
```

**Verify it's gone:**
```bash
curl -s "${MDK_BASE}/agents" -H "Authorization: Bearer ${MDK_TOKEN}" \
    | python3 -m json.tool | grep '"name":'
```

`smoke-bot` should not appear in the output.

**Angular notes:**
- 200 on success, 404 if the agent doesn't exist.
- Show a confirmation dialog in the UI — the soft-delete is
  recoverable but the user shouldn't think of it as easy undo from
  their side.

---

## Section 7 — Coming soon (GitHub publish + history)

Two endpoints are advertised in `/openapi.json` today but return 503
until ops registers the GitHub App for Movate:

### 7.1 — `POST /api/v1/agents/{name}/publish`

**Today:** Returns 503 with `code: "agent_persistence_unavailable"`
and a message pointing at the env-var setup required.

```bash
curl -s -X POST "${MDK_BASE}/api/v1/agents/faq-agent/publish" \
    -H "Authorization: Bearer ${MDK_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{}' | python3 -m json.tool
```

**Expected:**
```json
{
    "detail": {
        "error": {
            "code": "agent_persistence_unavailable",
            "message": "github integration is disabled; set MDK_GITHUB_ENABLED=1 ..."
        }
    }
}
```

**When live:** Pushes the agent's canonical bundle to a per-tenant
GitHub repo (`mova-io-agents-<tenant>`) as one commit. Returns
`{commit_sha, commit_url, branch, files_changed}` for the "Published"
toast in the UI.

### 7.2 — `GET /api/v1/agents/{name}/history`

**Today:** Same 503 shape as 7.1.

**When live:** Returns the agent's commit history (50 per page).
Each row: `{sha, message, author_name, author_email, timestamp,
html_url}`. Drives the "version history" panel.

**Angular notes:**
- Both routes are already in `/openapi.json` so your `npm run
  client:gen` (or equivalent) picks up typed methods today. They'll
  start returning real data the moment ops flips the flag — no
  client changes required.

---

## Section 8 — Troubleshooting

| You see | Likely cause | Fix |
|---|---|---|
| `401 {"detail": {"error": {"code": "auth_required"}}}` | Bearer missing, malformed, expired, or revoked | Re-set `MDK_TOKEN` from the onboarding bundle. If it's still 401, ping the team for a fresh bearer. |
| `404 {"detail": {"error": {"code": "not_found"}}}` on a run | The agent name in the URL doesn't exist | List agents via Section 2.1 to confirm the spelling |
| `unknown_agent` error on `GET /jobs/{id}` after creating an agent via wizard | Cross-pod filesystem isolation (item 109) | Use `?wait=true` on the run instead (Section 4.1) |
| `503 {"code": "agent_persistence_unavailable"}` on `/publish` or `/history` | GitHub integration not enabled yet | Expected; nothing to fix on your side. Will go away when ops registers the GitHub App. |
| `429 {"detail": {"error": {"code": "rate_limited"}}}` | Hit the per-bearer rate limit (60 req/min default) | Slow your polling, or ping the team for a higher limit |
| `422 {"detail": {"error": {"code": "invalid_bundle"}}}` on create | Body fails schema validation | Check the error message — usually a missing required field |
| Connection timeout / DNS error | Wrong `MDK_BASE` or runtime is down | Verify URL; check `/healthz` from Section 1.1 |

If you hit something not in this table, drop the curl + the full
response (status code + body) in Slack and the MDK team will diagnose.

---

## Checklist

Once you've worked through every section above, you've personally
exercised:

- [ ] Section 1: `/healthz` + `/ready`
- [ ] Section 2: `/agents` + `/api/v1/agents/{name}`
- [ ] Section 3: `/api/v1/agents/from-wizard` + `/validate`
- [ ] Section 4: All 6 run-related endpoints (inline + async + jobs + runs + trace)
- [ ] Section 5: Eval kickoff + scorecard + history
- [ ] Section 6: Delete + verification
- [ ] Section 7: Confirmed the 503 shape on GitHub endpoints

If every box is ticked, you're ready to wire these into the Mova iO
Angular client. The OpenAPI spec at `${MDK_BASE}/api/v1/openapi.json`
gives you typed signatures for every route above — point your
client-generator at it and you have the typed service layer in one
command.

Welcome aboard.
