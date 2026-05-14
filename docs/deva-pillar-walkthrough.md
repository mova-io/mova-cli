# MDK backend — pillar-by-pillar walkthrough for Mova iO

> Audience: Deva + the Mova iO Angular team
> Time to complete: ~20 minutes hands-on
> Outcome: You've personally hit every endpoint the Mova iO front
> end needs, you know which pillar each one belongs to, and you've
> seen the response shape your Angular typed client will parse.

The MDK backend surfaces 12 v1 endpoints organized into four pillars:

1. **Agent creation** (5) — onboard, inspect, validate, run
2. **Evals** (3) — kick off, fetch scorecard, list history
3. **Observability** (2) — per-run timeline, cross-run job feed
4. **Auth + status** (2) — job-state polling, health probes

This walkthrough exercises each, in dependency order, so by the end
you've touched every endpoint with a working bearer + a real agent
on the live runtime.

---

## What just changed (Wed EOD migration note)

The runtime moved this week from a personal pay-as-you-go subscription
to Movate's `AZLABSV2.0-Sandbox(POC)`. Practically:

- **New URL + new bearer** — replace your front-end env config with
  the values in the connection card below and retest one curl to
  confirm.
- **Fresh DB** — no carry-over of this week's exploratory runs.
  Wizard agents you created on the old runtime are gone; recreate
  via `POST /api/v1/agents/from-wizard`.
- **Three new endpoints since you last saw the spec:**
  - `DELETE /api/v1/agents/{name}` — soft-delete an agent
  - `POST   /api/v1/agents/{name}/publish` — push to GitHub
    (feature-flagged; returns 503 until ops registers the App)
  - `GET    /api/v1/agents/{name}/history` — GitHub commit log
    (feature-flagged; returns 503 today)
- **What stayed identical:** every endpoint you've already integrated
  keeps the same shape. Bearer auth, CORS, rate limiting, JSON
  conventions — all unchanged. The `POST /api/v1/agents/from-wizard`
  adapter still accepts the same wizard payload.

The old runtime is still up as a fallback through Friday — same old
URL + old bearer if you need to A/B against the new one. It gets
spun down once you confirm the new runtime works in your live flow.

---

## Section 0 — Setup (once per shell session)

### Your connection info

```
Runtime URL:    ${MDK_BASE}
OpenAPI spec:   ${MDK_BASE}/api/v1/openapi.json
Bearer token:   ${MDK_TOKEN}
Tenant ID:      ${MDK_TENANT_ID}
CORS allow:     http://localhost:4200 (send your prod hostname to
                add it; one-line CORS update on our side)
```

If you're reading the GitHub-committed version of this doc, those are
placeholders — the real values arrive via Teams DM / email from the
MDK team. **Treat the bearer like a password:** don't commit it,
don't paste it in tickets or screenshots, don't store it in a
non-gitignored file. If a previously-good request starts returning
401 the key was revoked — ping the team for a new one (~30 seconds
to mint).

### Export to your shell

```bash
export MDK_BASE="<runtime-url-from-the-bundle>"
export MDK_TOKEN="<bearer-from-the-bundle>"
```

Verify both are set without echoing the token:

```bash
echo "${MDK_BASE}"
echo "${MDK_TOKEN}" | head -c 20 && echo "..."
```

Every curl in this doc uses `${MDK_BASE}` and `${MDK_TOKEN}` — paste
either the env-var form (after the exports above) OR the literal
values from your bundle. Both run the same request.

### One helper — JSON pretty-printing

Every smoke pipes responses through `python3 -m json.tool` so the
JSON renders readably. Python 3 is pre-installed on macOS and Linux.
`jq` works too if you prefer.

---

## 60-second smoke (before the deep dive)

Confirm the runtime is alive + your bearer is valid before going
deeper. If any of these fail, stop and ping the MDK team:

```bash
# 1. Runtime is up
curl -s "${MDK_BASE}/healthz" | python3 -m json.tool

# 2. Storage is reachable (Postgres + KV)
curl -s "${MDK_BASE}/ready" | python3 -m json.tool

# 3. Bearer is accepted + agents are visible
curl -s "${MDK_BASE}/agents" -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d[\"agents\"])} agents visible')"
```

Expected:
- `/healthz` → `{"status": "ok", "version": "0.7.0"}`
- `/ready` → `{"status": "ready", "version": "0.7.0", "checks": {"storage": "ok"}}`
- `/agents` → `6 agents visible` (the baked-in catalog)

If all three pass, continue.

---

# Pillar 1 — Agent creation (5 endpoints)

Everything Mova iO needs to onboard a user's agent, inspect it,
gate it for production, and run it.

---

## 1.1 — `POST /api/v1/agents/from-wizard` (Mova iO's primary create path)

**Pillar:** Agent creation
**Mova iO use case:** the "Onboard Agent" wizard's submit button
**Wire shape:** JSON body (NOT multipart)
**Returns:** 201 on success, 409 if name collides, 422 on schema failure

This is the endpoint you call when the user clicks **Submit** in the
wizard. The body is a JSON object matching the wizard's field set;
the runtime translates wizard fields into a canonical agent bundle
on disk.

```bash
curl -X POST "${MDK_BASE}/api/v1/agents/from-wizard" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "smoke-bot",
    "agent_provider": "Movate",
    "agent_type": "Task Agent",
    "role": "FAQ assistant",
    "description": "Demo agent for the Mova iO walkthrough",
    "agent_role": "You are a friendly assistant that answers questions concisely.",
    "agent_goal": "Resolve user questions in 1-2 sentences.",
    "agent_prompt": "Question: {{ input.input }}\n\nAnswer:",
    "reference_output": "A clear, concise answer.",
    "mcp_connectors": [],
    "knowledge_store": "",
    "ai_model": "openai/gpt-4o-mini-2024-07-18",
    "ai_foundation": "azure"
  }' | python3 -m json.tool
```

**Expected response (201 Created):**

```json
{
  "name": "smoke-bot",
  "version": "0.1.0",
  "description": "Demo agent for the Mova iO walkthrough",
  "agent_dir": "smoke-bot",
  "files_persisted": ["agent.yaml", "prompt.md", "schema/input.json", "schema/output.json"]
}
```

**Angular notes:**

- **`agent_role` is the prompt body**, not the marketplace role label.
  The marketplace label is the `role` field (the chip text in your
  catalog). Different fields, easy to swap by mistake — type checking
  via the generated client catches it.
- The runtime auto-generates default I/O schemas: `{ "input":
  "string" }` for input, `{ "output": "string" }` for output. The
  agent's run-time input shape uses these names — see endpoint 1.5.
- The `name` is slugified server-side: lowercase, hyphens, no
  whitespace. `"My First Bot"` becomes `"my-first-bot"`. Send the
  raw display name; the slug comes back in the `name` field of the
  response.
- 422 errors carry a structured error body — render `error.message`
  inline in your form.

---

## 1.2 — `POST /api/v1/agents` (canonical multipart bundle upload)

**Pillar:** Agent creation
**Mova iO use case:** power-user upload for typed schemas + custom
prompts that don't fit the wizard
**Wire shape:** multipart/form-data — EITHER a zipped `bundle` field
OR four individual files (`agent_yaml`, `prompt`, `input_schema`,
`output_schema`) + optional `dataset`
**Returns:** 201 on success, 400 on malformed multipart, 422 on
schema failure

Skip this on your first pass — `/from-wizard` covers 95% of the
flow. Use this endpoint later when a tenant wants to push fully
typed I/O schemas or import an agent from another MDK installation.

The wire shape is multipart with these field names (zip mode):

```bash
curl -X POST "${MDK_BASE}/api/v1/agents" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -F "bundle=@my-agent.zip"
```

Individual-files mode:

```bash
curl -X POST "${MDK_BASE}/api/v1/agents" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -F "agent_yaml=@agent.yaml" \
  -F "prompt=@prompt.md" \
  -F "input_schema=@schema/input.json" \
  -F "output_schema=@schema/output.json" \
  -F "dataset=@evals/dataset.jsonl"
```

**Returns the same `AgentCreatedView` shape as 1.1** so your
client handles both create paths with the same type.

---

## 1.3 — `GET /api/v1/agents/{name}` (full profile)

**Pillar:** Agent creation
**Mova iO use case:** the agent-profile screen — render the prompt,
schemas, dataset summary, marketplace metadata, files-on-disk list
**Wire shape:** GET, no body
**Returns:** 200 with full profile, 404 if the agent doesn't exist

```bash
curl -s "${MDK_BASE}/api/v1/agents/smoke-bot" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool
```

**Expected response (excerpt — full body has ~20 fields):**

```json
{
  "name": "smoke-bot",
  "version": "0.1.0",
  "description": "Demo agent for the Mova iO walkthrough",
  "model": {
    "provider": "openai/gpt-4o-mini-2024-07-18"
  },
  "input_schema": {
    "type": "object",
    "properties": { "input": { "type": "string" } },
    "required": ["input"],
    "additionalProperties": false
  },
  "output_schema": { /* ... */ },
  "role": "faq-assistant",
  "persona": "You are a friendly assistant that answers questions concisely.",
  "capabilities": [],
  "skills": [],
  "contexts": [],
  "dataset": null,
  "timeout_call_ms": 30000,
  "timeout_total_ms": 60000,
  "max_cost_usd_per_run": 1.0,
  "agent_dir": "smoke-bot",
  "files": ["agent.yaml", "prompt.md", "schema/input.json", "schema/output.json"]
}
```

**Angular notes:**

- The full response can be 2-5 KB. Cache by `(name, version)` for
  the session.
- `dataset` is `null` for wizard-created agents until someone
  uploads an `evals/dataset.jsonl` (planned for a future endpoint —
  item 111 in our backlog).
- `files` lists the bundle's on-disk contents — useful for a "Files
  in this agent" UI panel. The set varies by creation path (wizard
  vs multipart); your client should treat it as a generic string
  array, not hard-code names.

---

## 1.4 — `POST /api/v1/agents/{name}/validate` (shippability gate)

**Pillar:** Agent creation
**Mova iO use case:** the green/yellow/red lint indicator next to
the prompt textarea; pre-deploy gate that blocks "Publish" if errors
**Wire shape:** POST, no body
**Returns:** 200 with `{passed, errors, warnings, cost_forecast}`

```bash
curl -X POST "${MDK_BASE}/api/v1/agents/smoke-bot/validate" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool
```

**Expected response (happy):**

```json
{
  "passed": true,
  "errors": [],
  "warnings": [],
  "cost_forecast": null
}
```

**Expected response with a lint issue:**

```json
{
  "passed": false,
  "errors": [
    {
      "code": "TINY_PROMPT",
      "severity": "error",
      "message": "prompt.md is only 12 characters; typical agent prompts are 200+",
      "hint": "expand the prompt with role, output schema, examples"
    }
  ],
  "warnings": [],
  "cost_forecast": null
}
```

**Angular notes:**

- `errors[]` and `warnings[]` are each `{code, severity, message,
  hint}` objects. Render `message` inline; surface `hint` as a
  tooltip on hover.
- `cost_forecast` is `null` for agents without a dataset; populated
  with `{estimated_total_usd, per_case_usd, sample_count}` once
  someone uploads eval cases (lets the UI show "this eval will
  cost ~$0.45 across 30 cases" before the user clicks Run).
- Call this after every prompt save in your wizard — gives the user
  immediate feedback before they click Publish.

---

## 1.5 — `POST /api/v1/agents/{name}/runs` (agent-scoped run)

**Pillar:** Agent creation
**Mova iO use case:** the "Test this agent" button + production run
calls from the chat UI
**Wire shape:** POST, JSON body `{input, notify_email?, mock?}`
**Returns:**
  - 202 + `{job_id}` in async mode (default — for queue/poll flow)
  - 200 + full `RunView` in inline mode (`?wait=true` — for
    synchronous "I want the answer NOW")
**Two modes — pick based on the agent type:**

| Agent created via | Use this run mode |
|---|---|
| `/from-wizard` (1.1) | **`?wait=true`** — required, see note below |
| Baked-in catalog agents | Either mode |

The reason: wizard-created agents land on the API pod's filesystem,
which the worker pod can't see (cross-pod isolation in Azure
Container Apps; item 109 in our backlog). `?wait=true` runs the
agent inline on the API pod and sidesteps the worker entirely. The
worker returns an `unknown_agent` error with a hint field pointing
you here if you get this wrong.

### 1.5a — Inline mode (the one Mova iO uses for wizard agents)

```bash
curl -X POST "${MDK_BASE}/api/v1/agents/smoke-bot/runs?wait=true" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"input": {"input": "What is Movate?"}}' \
  | python3 -m json.tool
```

**Expected response (200 OK):**

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
  "error": null,
  "created_at": "2026-05-14T15:21:56Z"
}
```

**Save the `run_id`** — you'll use it in Pillar 3 (Observability)
for the trace endpoint.

### 1.5b — Async mode (for baked-in agents, or future cross-pod-fixed setup)

```bash
curl -X POST "${MDK_BASE}/api/v1/agents/faq-agent/runs" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"input": {"question": "what is movate?"}}' \
  | python3 -m json.tool
```

**Expected response (202 Accepted):**

```json
{"job_id": "<uuid>", "status": "queued"}
```

Save the `job_id` for the polling step (`GET /jobs/{id}` in Pillar 4).

**Angular notes:**

- HTTP timeout on your client should be 60s. Inline mode holds the
  connection until the LLM returns; slow models take 20-30s for
  long outputs.
- `metrics.cost_usd` is in dollars — format as `$0.000013` (6 decimal
  places) for display.
- `metrics.latency_ms` is the LLM-call time, not the wall-clock
  request time. Add 200-500ms for queue + persistence overhead if
  you want the full trip.
- Input field name (the inner `input.<field>`) depends on the agent
  type:
  - Wizard agents: `{"input": "..."}`
  - `mdk init` template agents: `{"text": "..."}`
  - Custom agents: whatever their input schema declares
  Call 1.3 first if you're unsure — `input_schema.required` lists
  the field names.

---

# Pillar 2 — Evals (3 endpoints) ← Mova iO got these this week (PR #104)

Score the agent against a labeled dataset. Three endpoints work
together: kick off, fetch scorecard, list history per agent.

---

## 2.1 — `POST /api/v1/agents/{name}/evals` (kickoff)

**Pillar:** Evals
**Mova iO use case:** the "Run Eval" button on the agent profile
**Wire shape:** POST, JSON `{gate, runs, mock, judge?}`
**Returns:** 202 + `{eval_id, status, message}`

```bash
curl -X POST "${MDK_BASE}/api/v1/agents/faq-agent/evals" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"gate": 0.7, "runs": 1, "mock": true}' \
  | python3 -m json.tool
```

**Expected response (agent has a dataset):**

```json
{
  "eval_id": "0424edcc-f693-469f-8c5b-cc377432106f",
  "status": "success",
  "message": ""
}
```

**Expected response (agent has no dataset — common for wizard agents):**

```json
{
  "eval_id": "",
  "status": "failed",
  "message": "agent smoke-bot has no evals.dataset configured"
}
```

**Angular notes:**

- `mock: true` runs against a deterministic mock provider — useful
  for cheap CI / smoke. Use `mock: false` (or omit) for real LLM eval.
- `gate` is the pass threshold (0.0-1.0). If `mean_score` falls
  below it, the resulting scorecard's gate semantics flip to "fail"
  for CI purposes; the scorecard itself is still produced either way.
- `runs` is the per-case repeat count for averaging across LLM
  non-determinism. Default 1; bump to 3-5 for nontrivial signals.
- Wizard-created agents return `status: failed` until someone
  uploads `evals/dataset.jsonl`. A future endpoint (item 111) will
  POST a dataset to an existing agent; until then, an agent's
  dataset has to land at create time via the multipart path (1.2).

---

## 2.2 — `GET /api/v1/evals/{eval_id}` (scorecard)

**Pillar:** Evals
**Mova iO use case:** the eval-results screen — score chip + per-case
breakdown + dimensional ratings (accuracy / faithfulness / coverage /
latency)
**Wire shape:** GET, no body
**Returns:** 200 with full scorecard

```bash
curl -s "${MDK_BASE}/api/v1/evals/0424edcc-f693-469f-8c5b-cc377432106f" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool
```

**Expected response:**

```json
{
  "eval_id": "0424edcc-f693-469f-8c5b-cc377432106f",
  "agent": "faq-agent",
  "agent_version": "0.1.0",
  "dataset_hash": "bb6fdce3caa73a50c2c43993dbf46539b...",
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

- `judge_method` is `"exact"` (string match) or `"llm"` (different-
  family LLM judges the response). Render the method as a chip on
  the scorecard so users know whether they're seeing "did the answer
  match exactly" vs "did a judge model rate the answer".
- `mean_score` is the aggregate quality score (0.0-1.0); `pass_rate`
  is the fraction of cases above `threshold`. Render both — they tell
  different stories.
- `total_cost_usd` is the eval's cumulative LLM spend; show next to
  the score chip so users see the price tag.

---

## 2.3 — `GET /api/v1/evals?agent={name}` (history per agent)

**Pillar:** Evals
**Mova iO use case:** the eval-history panel on the agent profile —
trend line of `mean_score` over time, last-N evals
**Wire shape:** GET with `?agent=<name>` query param
**Returns:** 200 with `{evals: [...], count: N}`

```bash
curl -s "${MDK_BASE}/api/v1/evals?agent=faq-agent" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool
```

**Expected response:**

```json
{
  "evals": [
    {
      "eval_id": "0424edcc-...",
      "agent": "faq-agent",
      "agent_version": "0.1.0",
      "dataset_hash": "bb6fdce3...",
      "judge_method": "exact",
      "runs_per_case": 1,
      "threshold": 0.7,
      "mean_score": 0.85,
      "pass_rate": 0.9,
      "sample_count": 10,
      "total_cost_usd": 0.0034,
      "created_at": "2026-05-14T15:22:28Z"
    }
  ],
  "count": 1
}
```

**Angular notes:**

- Sorted newest-first by `created_at`. Use the first N for a
  sparkline; show the most recent's `mean_score` as the current
  score.
- Each entry has enough metadata to render the trend without
  follow-up `/evals/{id}` calls per row — only fetch the full
  scorecard when the user clicks a row.

---

# Pillar 3 — Observability (2 endpoints)

Show the user what happened inside a run, and let them browse jobs
across all runs filtered by status / agent.

---

## 3.1 — `GET /api/v1/runs/{run_id}/trace` (per-run timeline)

**Pillar:** Observability
**Mova iO use case:** the "Trace" tab on a run-result panel —
prompt, response, provider metadata, every span for workflow runs
**Wire shape:** GET, no body
**Returns:** 200 with `{kind, run, workflow, nodes, total_cost_usd, total_latency_ms}`

```bash
curl -s "${MDK_BASE}/api/v1/runs/<run_id>/trace" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool
```

**Expected response (single-agent run):**

```json
{
  "kind": "agent",
  "run": {
    "run_id": "<uuid>",
    "agent": "faq-agent",
    "provider": "openai/gpt-4o-mini-2024-07-18",
    "status": "success",
    "input": {"question": "what is movate?"},
    "output": {"answer": "Movate is...", "confidence": 0.9},
    "metrics": {
      "latency_ms": 2972,
      "cost_usd": 0.000036,
      "tokens": {"input": 101, "output": 35, "cached_input": 0}
    },
    "prompt_hash": "177f61e770c9...",
    "created_at": "2026-05-14T15:13:00Z"
  },
  "workflow": null,
  "nodes": [],
  "total_cost_usd": 0.000036,
  "total_latency_ms": 2972
}
```

**Expected response (workflow run):**

```json
{
  "kind": "workflow",
  "run": null,
  "workflow": {
    "workflow_run_id": "<uuid>",
    "workflow_name": "returns-pipeline",
    "status": "success"
  },
  "nodes": [
    {"node_id": "validate", "run": { /* ... */ }},
    {"node_id": "extract",  "run": { /* ... */ }}
  ],
  "total_cost_usd": 0.000089,
  "total_latency_ms": 4521
}
```

**Angular notes:**

- Branch your trace-viewer component on `kind === "workflow"` — same
  endpoint, two shapes. Single-agent runs have `workflow: null` and
  empty `nodes`; workflow runs flip them.
- `prompt_hash` is sha256 of the rendered prompt — useful for the
  "prompt didn't change between runs" badge.

---

## 3.2 — `GET /api/v1/jobs?agent=&status=` (filterable history)

**Pillar:** Observability
**Mova iO use case:** the cross-run activity feed — "all my agents
that errored in the last hour", "what's still running", "show me
recent successes"
**Wire shape:** GET with optional `?agent=`, `?status=`, `?limit=`
query params
**Returns:** 200 with `{jobs: [...], count: N}`

```bash
# All recent jobs (default limit)
curl -s "${MDK_BASE}/jobs?limit=10" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool

# Only this agent's jobs
curl -s "${MDK_BASE}/jobs?agent=faq-agent&limit=20" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool

# Only failed jobs across all agents
curl -s "${MDK_BASE}/jobs?status=error&limit=50" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool
```

**Expected response:**

```json
{
  "jobs": [
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
  ],
  "count": 1
}
```

**Angular notes:**

- Scoped to your tenant by the bearer — you'll never see another
  tenant's jobs. Safe to call freely.
- Sorted newest-first.
- Status values: `queued | running | success | error | dead_letter |
  safety_blocked`. The last two are terminal-but-not-success; render
  them differently from `success`.
- `error` (when present) carries `{type, message, retryable, hint}`.
  The `hint` field is operator-facing — surface as a tooltip if your
  UI can show it.

---

# Pillar 4 — Auth + status (2 endpoints)

The plumbing endpoints — job polling for the async run path and
unauthed health probes.

---

## 4.1 — `GET /jobs/{job_id}` (single-job state)

**Pillar:** Auth + status
**Mova iO use case:** polling loop when you submitted an async run
in 1.5b — call every 1-2 seconds until `status` is terminal, then
fetch the full output via `GET /runs/{result_run_id}` (a separate
endpoint not in this walkthrough but identical to the `run` shape
inside 3.1's response).
**Wire shape:** GET, no body
**Returns:** 200 with full job state, 404 if no such job_id

```bash
curl -s "${MDK_BASE}/jobs/<job_id>" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool
```

**Expected response (terminal success):**

```json
{
  "job_id": "<uuid>",
  "kind": "agent",
  "target": "faq-agent",
  "status": "success",
  "input": {"question": "..."},
  "result_run_id": "<uuid>",
  "error": null,
  "created_at": "2026-05-14T15:12:57Z",
  "claimed_at": "2026-05-14T15:12:57Z",
  "completed_at": "2026-05-14T15:13:00Z"
}
```

**Expected response (terminal error):**

```json
{
  "job_id": "<uuid>",
  "kind": "agent",
  "target": "smoke-bot",
  "status": "error",
  "input": {"input": "..."},
  "result_run_id": null,
  "error": {
    "type": "unknown_agent",
    "message": "agent 'smoke-bot' not registered on this worker",
    "retryable": false,
    "hint": "use ?wait=true to run inline on the API pod (item 110)"
  },
  "created_at": "...",
  "claimed_at": "...",
  "completed_at": "..."
}
```

**Angular notes:**

- Stop polling on any terminal status (`success | error |
  dead_letter | safety_blocked`); don't keep polling forever.
- 1-2 second poll interval is fine; the underlying queue check runs
  on a similar cadence so faster polling wastes both sides' CPU.
- Once `status === "success"`, fetch the full output via
  `GET /runs/{result_run_id}` (uses the same shape that appears inside
  the trace response's `run` field).

---

## 4.2 — `GET /healthz` and `GET /ready` (probes, unauthed)

**Pillar:** Auth + status
**Mova iO use case:** status-page widget; "is the backend reachable
before I let the user click Submit"
**Wire shape:** GET, no body, no auth required
**Returns:**
  - `/healthz` → always 200 unless the runtime is fully dead
  - `/ready` → 200 with `status: "ready"` if DB ping passes; 503
    with `status: "not_ready"` + per-check detail otherwise

```bash
curl -s "${MDK_BASE}/healthz" | python3 -m json.tool
curl -s "${MDK_BASE}/ready"   | python3 -m json.tool
```

**Angular notes:**

- Don't put these inside an auth interceptor — they explicitly skip
  the bearer requirement so monitoring tools can hit them without
  rotating creds.
- `/ready` returning 503 means the runtime is reachable but the DB
  isn't. Show a "service degraded" banner; don't auto-retry the
  user's submission until `/ready` comes back 200.

---

# Coming soon — GitHub publish + history (2 endpoints, feature-flagged)

Both routes are already advertised in `/openapi.json` so your
`npm run client:gen` (or equivalent) picks up typed methods today.
They return 503 until ops registers the MDK GitHub App on the
Movate org. When that flips, you start getting real responses with
zero client changes.

### `POST /api/v1/agents/{name}/publish`

```bash
curl -X POST "${MDK_BASE}/api/v1/agents/faq-agent/publish" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -m json.tool
```

**Today (503):**

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

**When live:** pushes the agent's bundle to a per-tenant GitHub
repo as one commit. Returns `{commit_sha, commit_url, branch,
files_changed[]}` for your "Published" toast + "View on GitHub"
link.

### `GET /api/v1/agents/{name}/history`

Same 503 today; will return paginated commit history. Each row:
`{sha, message, author_name, author_email, timestamp, html_url}`.
Drives a "version history" panel on the agent profile.

### `DELETE /api/v1/agents/{name}`

This one is NOT feature-flagged — it works today. Soft-deletes the
agent (moves the bundle to a sibling `.deleted-<name>-<timestamp>/`
directory; recoverable for ~7 days).

```bash
curl -X DELETE "${MDK_BASE}/api/v1/agents/smoke-bot" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | python3 -m json.tool
```

**Expected response:**

```json
{
  "name": "smoke-bot",
  "deleted_dir": ".deleted-smoke-bot-1778772217"
}
```

Show a confirmation dialog in your UI before calling this — the
soft-delete is recoverable on our side but the user shouldn't think
of it as easy undo from theirs.

---

# Troubleshooting — the 5 errors you'll actually hit

| Status | Error code | Cause | Fix |
|---|---|---|---|
| **401** | `auth_required` | Bearer missing, malformed, expired, or revoked | Re-set `MDK_TOKEN` from the onboarding bundle. If still 401, ping the MDK team for a fresh bearer. |
| **404** | `not_found` | Agent name in the URL doesn't exist | List agents (Pillar 1.3 / `GET /agents`) to confirm spelling |
| **422** | `invalid_bundle` | Body fails schema validation | Check `error.message` — usually a missing required field |
| **429** | `rate_limited` | Hit per-bearer limit (60 req/min default) | Slow polling, or ping the MDK team for a higher limit |
| **503** | `agent_persistence_unavailable` | GitHub integration not enabled (publish / history endpoints only) | Expected; nothing to fix on your side |

**Special case — `unknown_agent` error inside a 200 job poll:**
The error body's `hint` field tells you what to do. For wizard
agents on this runtime, it always says "use `?wait=true`". Surface
the hint as a tooltip in your job-error panel.

---

# Final checklist — confirm you've exercised every endpoint

Once you've worked through Pillars 1-4, you've touched:

- [ ] **1.1** `POST /api/v1/agents/from-wizard` — wizard agent created
- [ ] **1.2** `POST /api/v1/agents` (multipart) — skipped (use later)
- [ ] **1.3** `GET /api/v1/agents/{name}` — profile fetched
- [ ] **1.4** `POST /api/v1/agents/{name}/validate` — lint result seen
- [ ] **1.5** `POST /api/v1/agents/{name}/runs` — both modes (inline + async)
- [ ] **2.1** `POST /api/v1/agents/{name}/evals` — eval kicked off
- [ ] **2.2** `GET /api/v1/evals/{eval_id}` — scorecard fetched
- [ ] **2.3** `GET /api/v1/evals?agent=` — history fetched
- [ ] **3.1** `GET /api/v1/runs/{run_id}/trace` — timeline fetched
- [ ] **3.2** `GET /api/v1/jobs?agent=&status=` — filtered list fetched
- [ ] **4.1** `GET /jobs/{job_id}` — terminal job polled
- [ ] **4.2** `GET /healthz` + `/ready` — probes returned 200

If every box ticks, you're ready to wire these into the Mova iO
Angular client. Point your openapi-generator at:

```
${MDK_BASE}/api/v1/openapi.json
```

…and you get typed service methods for every endpoint above in one
command.

---

## Reach out

Anything not in this doc, or anything broken — Teams DM the MDK
team. For errors, paste the curl + the full response (status code +
body); makes diagnosis quick.

Welcome aboard.
