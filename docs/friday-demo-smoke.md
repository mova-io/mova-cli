# Friday Mova iO demo — smoke test runbook

End-to-end validation of MDK's v0.7 Angular-compatible endpoints
against the deployed Azure ACA runtime. Run this **before** the
Friday meeting to catch deployment drift.

Estimated time: **~5 minutes**.

## Prereqs

* You have the bearer token from
  [`scripts/friday-demo-deploy.sh`](../scripts/friday-demo-deploy.sh)
* `curl` + `jq` installed (most macOS/Linux machines have both)
* The runtime URL (printed at the end of the deploy script)

Export both as env vars for the rest of this runbook:

```bash
export MDK_BASE=https://movate-dev-api.victoriouswater-7958662f.eastus2.azurecontainerapps.io
export MDK_TOKEN="mvt_live_..."   # paste the bearer
```

## Step 1 — Liveness + readiness (no auth)

```bash
curl -sS "${MDK_BASE}/healthz" | jq .
# Expected: {"status": "ok"}

curl -sS "${MDK_BASE}/ready" | jq .
# Expected: {"status": "ok", "checks": {"storage": "ok"}}
```

If either returns non-200, the deploy is incomplete — re-run
`friday-demo-deploy.sh`.

## Step 2 — Confirm v0.7 routes are in the OpenAPI spec

```bash
curl -sS "${MDK_BASE}/openapi.json" \
  | jq '.paths | keys | map(select(startswith("/api/v1")))'
```

Expected list (10 paths):

```
/api/v1/agents
/api/v1/agents/from-wizard
/api/v1/agents/{name}
/api/v1/agents/{name}/evals
/api/v1/agents/{name}/runs
/api/v1/agents/{name}/validate
/api/v1/evals
/api/v1/evals/{eval_id}
/api/v1/jobs
/api/v1/runs/{run_id}/trace
```

If `/api/v1/agents/from-wizard` is missing, the old image is still
serving — check `az containerapp revision list -g movate-dev-rg -n movate-dev-api`.

## Step 3 — Create an agent via the wizard endpoint

Mimics what Deva's Angular UI will POST after the user fills the
Onboard Agent wizard:

```bash
curl -sS -X POST "${MDK_BASE}/api/v1/agents/from-wizard" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Smoke Test Agent",
    "agent_provider": "Movate",
    "agent_type": "Task Agent",
    "role": "Assistant",
    "description": "End-to-end smoke target for the Friday demo",
    "agent_role": "Concise, technical, JSON-only output",
    "agent_goal": "Echo back the input",
    "agent_prompt": "Respond ONLY with valid JSON: {\"output\": \"<echo>\"}\n\nInput: {{ input.input }}",
    "ai_model": "openai/gpt-4o-mini-2024-07-18",
    "ai_foundation": "Azure"
  }' | jq .
```

Expected response:

```json
{
  "name": "smoke-test-agent",
  "version": "0.1.0",
  "description": "End-to-end smoke target for the Friday demo",
  "agent_dir": "smoke-test-agent",
  "files_persisted": [
    "agent.yaml",
    "prompt.md",
    "schema/input.json",
    "schema/output.json"
  ]
}
```

Note the name slugification: "Smoke Test Agent" → "smoke-test-agent".

## Step 4 — Fetch the agent profile

Drives the Angular agent-profile view:

```bash
curl -sS "${MDK_BASE}/api/v1/agents/smoke-test-agent" \
  -H "Authorization: Bearer ${MDK_TOKEN}" | jq '{
    name, version, role, persona, model_provider,
    prompt_hash, files,
    capabilities, tags
  }'
```

Expected: full profile including the marketplace metadata, prompt
hash, and canonical files list.

## Step 5 — Validate the agent

The "is this shippable?" gate:

```bash
curl -sS -X POST "${MDK_BASE}/api/v1/agents/smoke-test-agent/validate" \
  -H "Authorization: Bearer ${MDK_TOKEN}" | jq .
```

Expected: `passed: true` (the prompt mentions JSON and references
the `output` schema field). Warnings empty.

## Step 6 — Run the agent (mock provider)

```bash
JOB_ID=$(curl -sS -X POST "${MDK_BASE}/api/v1/agents/smoke-test-agent/runs" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"input": {"input": "hello world"}}' \
  | jq -r .job_id)
echo "job_id: ${JOB_ID}"

# Poll until terminal (worker should claim within ~1s):
curl -sS "${MDK_BASE}/jobs/${JOB_ID}" \
  -H "Authorization: Bearer ${MDK_TOKEN}" | jq '{status, target, error}'
```

Expected: `status` transitions queued → running → success within
5 seconds. (Failure mode: worker isn't draining; check
`az containerapp logs show -g movate-dev-rg -n movate-dev-worker`.)

## Step 7 — Kick off + retrieve an eval

For demo purposes use `mock=true` so the eval is deterministic +
sub-second. **Note**: the smoke agent created above has no eval
dataset — for this step, either upload one via the multipart
endpoint, OR run the eval against an existing agent that has a
dataset. Replace `<AGENT_WITH_DATASET>` below:

```bash
EVAL_ID=$(curl -sS -X POST \
  "${MDK_BASE}/api/v1/agents/<AGENT_WITH_DATASET>/evals" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"gate": 0.0, "runs": 1, "mock": true}' \
  | jq -r .eval_id)
echo "eval_id: ${EVAL_ID}"

# Retrieve the scorecard:
curl -sS "${MDK_BASE}/api/v1/evals/${EVAL_ID}" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | jq '{eval_id, agent, mean_score, pass_rate, sample_count}'
```

Expected: a populated scorecard with non-zero `sample_count` and
`mean_score`/`pass_rate` between 0 and 1.

## Step 8 — Trace a run

Drives the Angular trace-viewer:

```bash
# Find a recent run_id for the smoke agent:
RUN_ID=$(curl -sS \
  "${MDK_BASE}/api/v1/jobs?agent=smoke-test-agent" \
  -H "Authorization: Bearer ${MDK_TOKEN}" \
  | jq -r '.jobs[0].result_run_id // empty')
echo "run_id: ${RUN_ID}"

# Fetch the trace:
curl -sS "${MDK_BASE}/api/v1/runs/${RUN_ID}/trace" \
  -H "Authorization: Bearer ${MDK_TOKEN}" | jq '{
    kind,
    run: .run | {status, agent, metrics},
    total_cost_usd,
    total_latency_ms
  }'
```

Expected: `kind: "agent"`, full run dict with metrics, totals
populated.

## Step 9 — CORS preflight from a non-MDK origin

Confirms the Angular dev server can reach the runtime:

```bash
curl -sS -X OPTIONS "${MDK_BASE}/api/v1/agents" \
  -H "Origin: http://localhost:4200" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: Authorization, Content-Type" \
  -i | grep -i "access-control-"
```

Expected headers:

```
access-control-allow-origin: http://localhost:4200
access-control-allow-methods: GET, POST, PUT, DELETE, PATCH, OPTIONS
access-control-expose-headers: X-RateLimit-Limit, X-RateLimit-Remaining, ...
```

If the `access-control-allow-origin` is absent, the CORS env var
didn't take — re-run the relevant step in
`friday-demo-deploy.sh` (the `az containerapp update --set-env-vars`
call).

## Step 10 — Cleanup (optional)

Delete the smoke agent so the Friday demo starts clean:

```bash
# (No DELETE endpoint in v0.7 — manual cleanup via az containerapp exec)
az containerapp exec \
  -g movate-dev-rg \
  -n movate-dev-api \
  --command "rm -rf /home/movate/agents/smoke-test-agent"
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/healthz` 503 | Postgres not reachable | `az postgres flexible-server show -g movate-dev-rg -n movate-dev-pg-jsy --query state` |
| `/api/v1/from-wizard` 404 | Old image still serving | `az containerapp revision list -g movate-dev-rg -n movate-dev-api` — confirm latest revision is the deploy's image tag |
| 401 with valid bearer | Token revoked or wrong env | `az containerapp exec -g movate-dev-rg -n movate-dev-api --command "movate auth list-keys"` |
| 422 on wizard POST | Body missing required fields | Confirm `name`, `agent_prompt`, `ai_model` all present + non-empty |
| Eval kickoff hangs >30s with mock=true | EvalEngine import time on cold container | Cold start; subsequent calls are fast. Run the smoke twice to warm. |
| CORS error from browser | Origin not in `MDK_CORS_ALLOWED_ORIGINS` | Add it via `az containerapp update --set-env-vars MDK_CORS_ALLOWED_ORIGINS=...,<new>` |

## What this runbook proves

Running this end-to-end confirms:

1. The deployed runtime serves v0.7 (not a cached v0.5 image)
2. Auth works (key not revoked, tenant scoping correct)
3. Wizard JSON → canonical bundle translation works on the real
   runtime (not just in tests)
4. Agent runs reach the worker and produce a RunRecord
5. Eval kickoff persists an EvalRecord retrievable by id
6. Trace endpoint returns structured timeline data
7. CORS allows Deva's origins

If any step fails, the Friday meeting is **not** demo-ready. Run
this 30 minutes before the meeting at minimum.
