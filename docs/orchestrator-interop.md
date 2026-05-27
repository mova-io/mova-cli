# Orchestrator interop — drive a movate agent/workflow as a task

**Status:** implements ADR 017 Decision 3 (D3).
**Posture (binding):** movate stays the **callable**, never the
**dependent**. There is **no** orchestrator in movate's core dependencies.
A team that already runs Prefect / Airflow / Dagster / Azure Data Factory /
Logic Apps / Step Functions drives a movate agent or workflow as a *task*
over the existing async API — and movate takes no dependency back.

This page is the source of truth for the three interop paths:

1. **Prefect** — `mdk[prefect]` task wrapper (thin, optional).
2. **Airflow** — `mdk[airflow]` `MovateAgentOperator` (thin, optional).
3. **Generic webhook / CLI** — the no-code contract for *any* tool
   (Dagster, ADF, Logic Apps, raw `curl`, `mdk submit`). No movate-side
   code required.

All three call the same API surface and return the agent's typed
`output` dict.

---

## The API contract every path uses

There are two equivalent ways to invoke a movate agent over HTTP; both are
already shipped (see `src/movate/runtime/app.py` and
`src/movate/core/client.py`).

### A. Async: submit → poll → fetch (production-grade)

This is the path the adapters and `mdk submit` use. It rides the durable
Postgres job queue + the KEDA worker pool, so it scales, retries, and
dead-letters; it works for **both** agents and workflows; and it never
holds an HTTP request open for the full agent duration.

| Step | Request | Returns |
|---|---|---|
| 1. Submit | `POST /run` with `{kind, target, input}` — or the REST-clean `POST /api/v1/agents/{name}/runs` (agent-only, `kind` implicit) | `202` + `{ "job_id": "...", "status": "queued" }` |
| 2. Poll | `GET /api/v1/jobs/{job_id}` (or unversioned `GET /jobs/{job_id}`) until `status` is terminal | `{ "status": "success" \| "error" \| ..., "result_run_id": "..." }` |
| 3. Fetch | `GET /api/v1/runs/{result_run_id}` (or unversioned `GET /runs/{run_id}`) | the full `RunView`, including the agent's typed `output` |

Terminal statuses: `success`, `error`, `safety_blocked`, `dead_letter`,
`cancelled`. Only `success` carries a usable `output`; the others are a
failed task.

`kind` is `agent` (default) or `workflow`. For a workflow, `input` is the
workflow's initial state and `target` is the workflow name.

### B. Inline: one synchronous call (`?wait=true`)

For a no-code tool that wants a single round-trip and can tolerate the HTTP
request blocking for the agent's duration:

```
POST /api/v1/agents/{name}/runs?wait=true
{ "input": { ... } }
```

Returns `200` + a `RunView` directly (no polling). This executes the agent
**in-process** in the API request rather than on the worker pool — fine for
short single-LLM-call agents, not for long tool-use loops or workflows.
Prefer path A for orchestrated pipeline steps; `?wait=true` exists for the
quick/demo case.

### Auth

Every call needs a bearer token in `Authorization: Bearer <token>`:

* an `mvt_*` runtime API key with the `run` (submit) + `read` (poll/fetch)
  scopes (mint via `mdk auth keys create`), or
* a federated OIDC JWT if the runtime is configured to accept one
  (`MOVATE_OIDC_ISSUER`, ADR 012 D3).

---

## 1. Prefect (`mdk[prefect]`)

Install the optional extra:

```bash
uv pip install 'movate-cli[prefect]'
```

`movate.integrations.prefect.run_agent` IS a Prefect `@task` (Prefect is
imported lazily, so installing it is the only requirement). Call it inside
a `@flow`:

```python
from prefect import flow
from movate.integrations.prefect import run_agent, run_workflow

@flow
def triage_flow(ticket: dict):
    # Connection comes from MOVATE_RUNTIME_URL + MOVATE_API_KEY env
    # (the same wiring `mdk submit` uses), or pass base_url=/api_key=.
    triaged = run_agent("triage-bot", ticket)        # returns the output dict
    summary = run_agent("summarizer", triaged)       # chain the outputs
    return summary

# Drive a movate *workflow* (DAG) instead of a single agent:
@flow
def returns_flow(order: dict):
    return run_workflow("returns-pipeline", order)
```

Signature (both `run_agent` and `run_workflow`):

```python
run_agent(
    target: str,
    payload: dict,
    *,
    base_url: str | None = None,   # omit BOTH base_url + api_key to use env
    api_key: str | None = None,
    poll_interval: float = 1.0,
    poll_timeout: float | None = 300.0,  # None = wait forever
    notify_email: str | None = None,
) -> dict   # the agent's `output`
```

A failed/blocked run, a transport error, or a poll timeout raises
`movate.integrations.orchestration.OrchestrationError`, which Prefect
records as a failed task run (so Prefect's retries/observability apply).

---

## 2. Airflow (`mdk[airflow]`)

Install the optional extra (the dist is **`apache-airflow`**; the import
package is `airflow`):

```bash
uv pip install 'movate-cli[airflow]'
```

`MovateAgentOperator` is a thin `BaseOperator` subclass. Airflow is
imported lazily (the operator class is built on first access), so importing
`movate` never requires Airflow.

```python
from airflow import DAG
from airflow.utils.dates import days_ago
from movate.integrations.airflow import MovateAgentOperator

with DAG("ticket-triage", start_date=days_ago(1), schedule="@hourly") as dag:
    triage = MovateAgentOperator(
        task_id="triage",
        agent="triage-bot",
        # `payload` is templated — Jinja in the values is rendered first.
        payload={"ticket_id": "{{ dag_run.conf['ticket_id'] }}"},
        # base_url/api_key omitted → read MOVATE_RUNTIME_URL +
        # MOVATE_API_KEY from the worker env. Or pass them explicitly /
        # wire from an Airflow Connection.
        poll_timeout=600.0,
    )

    summarize = MovateAgentOperator(
        task_id="summarize",
        agent="summarizer",
        # Consume the upstream task's output via XCom.
        payload={"text": "{{ ti.xcom_pull(task_ids='triage')['summary'] }}"},
    )

    triage >> summarize
```

`execute()` returns the agent's `output` dict, which Airflow XCom-pushes
for downstream tasks. Pass `kind="workflow"` to drive a movate workflow.
A failed run raises `OrchestrationError` → Airflow marks the task failed
(its retries/SLAs/alerts apply).

---

## 3. Generic webhook / CLI — any tool, no movate-side code

Anything that can make an authenticated HTTP call (Dagster ops, Azure Data
Factory Web activity, Logic Apps HTTP action, AWS Step Functions, a cron +
`curl`, a CI job) can drive movate with **zero** movate-side code. Use
either API path above.

### Raw HTTP — inline (single call)

```bash
curl -sS -X POST \
  "$MOVATE_RUNTIME_URL/api/v1/agents/triage-bot/runs?wait=true" \
  -H "Authorization: Bearer $MOVATE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"ticket_id": "T-123"}}'
# → 200 + a RunView; read `.output` for the agent's result, `.status` for success/error.
```

### Raw HTTP — async (submit → poll → fetch)

```bash
# 1. Submit (agent). For a workflow: POST /run with {"kind":"workflow","target":"...","input":{...}}
JOB=$(curl -sS -X POST "$MOVATE_RUNTIME_URL/api/v1/agents/triage-bot/runs" \
  -H "Authorization: Bearer $MOVATE_API_KEY" -H "Content-Type: application/json" \
  -d '{"input": {"ticket_id": "T-123"}}' | jq -r .job_id)

# 2. Poll until terminal
while :; do
  BODY=$(curl -sS "$MOVATE_RUNTIME_URL/api/v1/jobs/$JOB" \
    -H "Authorization: Bearer $MOVATE_API_KEY")
  STATUS=$(echo "$BODY" | jq -r .status)
  case "$STATUS" in
    success|error|safety_blocked|dead_letter|cancelled) break ;;
  esac
  sleep 1
done

# 3. Fetch the output on success
RUN=$(echo "$BODY" | jq -r .result_run_id)
curl -sS "$MOVATE_RUNTIME_URL/api/v1/runs/$RUN" \
  -H "Authorization: Bearer $MOVATE_API_KEY" | jq .output
```

### `mdk submit` — the CLI as the task body

If the orchestrator can shell out, `mdk submit` does submit→poll→fetch for
you (it's the same `MovateClient` the adapters use):

```bash
# Configure the target once (stores URL + token-env binding):
mdk config add-target prod --url "$MOVATE_RUNTIME_URL"

# Run an agent and block for the result (exit 0 on success, 1 on failure,
# 124 on timeout — branchable in any orchestrator):
mdk submit triage-bot '{"ticket_id": "T-123"}' -t prod --wait --output json

# Run a workflow:
mdk submit returns-pipeline -t prod --kind workflow --input initial_state.json --wait
```

`--output json` prints a single parsable `{ "job": {...}, "run": {...} }`
envelope, so the orchestrator can read `.run.output` from the task's
stdout.

---

## Why no orchestrator in core (ADR 017 D1/D3)

movate already orchestrates in-platform: a workflow DAG engine
(`WorkflowRunner`), a durable Postgres job queue (`JobKind.AGENT/WORKFLOW/
EVAL/BENCH`), KEDA queue-depth autoscaling, retry + dead-letter, plus a
native scheduler/triggers and HITL pause/resume (D2/D5). Adopting Airflow
or Prefect *as a core dependency* would duplicate that with a heavier,
worse-fit stack and contradict ADR 001 (portability) + the minimal-deps
rule. So external orchestrators integrate by **calling** movate — the
adapters here are thin, optional, and lazy-imported, and the license gate
(`scripts/check_licenses.py --strict`) does **not** police the
`prefect`/`airflow` extras (they're outside `SHIPPED_EXTRAS`, exactly like
the `anthropic`/`openai`/`github` extras). If a deployment ever genuinely
needs a single external durable engine, that's ADR 017 D4 — Temporal or
Prefect behind an adapter, with Deva sign-off — not a core dependency.
```
