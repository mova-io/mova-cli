# MDK — Movate Development Kit

Declarative platform for building, evaluating, and deploying AI agents and workflows.

**Internal Movate framework.** Proprietary; private artifact distribution
only — see [RELEASING.md](RELEASING.md). Public PyPI is intentionally
not used.

The CLI is installed as both **`mdk`** (canonical) and **`movate`** (transitional alias —
dropped in a future major release). Every example in this README uses `mdk`; substitute
`movate` if you prefer, both work today.

## Status

| version | tag | what landed |
|---|---|---|
| 0.5.0 | [`v0.5.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.5.0) | HTTP runtime + worker + Postgres — movate is now a service |
| 0.4.0 | [`v0.4.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.4.0) | Observability + regression-detection (Langfuse, OTel, trace replay, eval baseline diff, run replay, CI eval-gate) |
| 0.3.1 | [`v0.3.1`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.1) | Workflow runner double-save fix |
| 0.3.0 | [`v0.3.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.0) | Sequential workflows (forward-aware IR + compiler + runner) |
| 0.2.0 | [`v0.2.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.2.0) | Eval engine (exact-match + LLM-as-judge with cross-family enforcement) |

**v1.0 next (`main`)** — Azure deploy + production hardening: Bicep IaC
(ACA + Postgres Flex + ACR + Key Vault), `mdk deploy`, model policy
enforcement, tenant isolation audit. See
[docs/v0.5-design.md](docs/v0.5-design.md) for the v0.5 architecture
that v1.0 builds on.

**v0.7 planned** — Microsoft Teams app for non-technical users: run +
eval Movate agents from inside a Teams channel, no CLI required.
Design captured in
[ADR 003 — Teams integration](docs/adr/003-teams-integration.md).

## What works today

| capability | command | status |
|---|---|---|
| Scaffold an agent | `mdk init <name> -t <template>` | ✓ v0.1 |
| Validate agent.yaml + schemas | `mdk validate <path>` | ✓ v0.1 |
| Run an agent locally | `mdk run <path> <input> [--mock]` | ✓ v0.1 |
| Per-provider pricing introspection | `mdk pricing` | ✓ v0.1 |
| Multi-model bench | `mdk bench <path>` | ✓ v0.2 |
| Eval suite + gating | `mdk eval <path> --gate 0.7` | ✓ v0.2 |
| Sequential workflow execution | `mdk run <workflow-path>` | ✓ v0.3 |
| Trace replay (agent + workflow) | `mdk trace replay <id>` | ✓ v0.4 |
| Regression detection vs baseline | `mdk eval --baseline <id>` <br> `mdk eval --baseline-file <path>` | ✓ v0.4 |
| Re-run a stored input against current code | `mdk run <path> --replay <run-id>` | ✓ v0.4 |
| API key issuance / revocation | `mdk auth create-key | list-keys | revoke-key` | ✓ v0.5 |
| HTTP runtime | `mdk serve` | ✓ v0.5 |
| Background worker | `mdk worker` | ✓ v0.5 |
| Postgres backend | `MOVATE_DB_URL=postgresql://...` | ✓ v0.5 |
| Submit jobs to a deployed runtime | `mdk submit <agent>` (with `--wait` + `--notify`) | ✓ v0.5+ |
| Inspect jobs on a deployed runtime | `mdk jobs show | wait | list-agents` | ✓ v0.5+ |
| Manage deployment targets | `mdk config add-target | use | list-targets` | ✓ v0.5+ |
| Azure deploy (Bicep IaC) | `infra/azure/main.bicep` (manual `az deployment`) | ✓ v1.0 stage 1 |
| One-command deploy to ACA | `mdk deploy --target <name>` | ✓ v1.0 stage 2 |
| Auto-deploy on push to release/* | `.github/workflows/deploy.yml` (federated OIDC) | ✓ v1.0 stage 2 |
| Azure preflight diagnostic | `mdk doctor --target <name>` | ✓ v1.0 |
| Azure bootstrap (RG + SP + federated cred) | `scripts/azure-bootstrap.sh <env>` | ✓ v1.0 |
| Job retry + dead-letter on transient failures | `JobRetryPolicy` + `mdk jobs list --status dead_letter` | ✓ post-v1.0 |
| Liveness + readiness probes for ACA | `GET /healthz` (cheap) + `GET /ready` (deep checks) | ✓ post-v1.0 |
| Per-API-key rate limiting | `mdk serve --rate-limit-per-minute 60` | ✓ post-v1.0 |
| Worker autoscaling on queue depth | KEDA postgresql scaler in `containerapp-worker.bicep` | ✓ post-v1.0 |
| Per-tenant monthly cost ceiling | `mdk tenants set-budget <id> --monthly-usd 500` | ✓ post-v1.0 |
| Model policy enforcement | `movate.yaml: policy:` (allowed_providers, deny_models, max cost) | ✓ v1.0 stage 3 |

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Install

`movate-cli` is **not** on public PyPI. Install from a private source:

```bash
# Direct from the GitHub repo (requires read access):
uv pip install "git+https://github.com/jeremyyuAWS/movate-cli.git"

# Or clone for development:
git clone https://github.com/jeremyyuAWS/movate-cli.git
cd movate-cli
uv sync --all-extras --dev
```

Distribution paths for tagged releases live in [RELEASING.md](RELEASING.md).

## Quickstart — 5 minutes through the inner loop

The hermetic path uses `--mock` so you don't need an API key.

### 1. Scaffold an agent

```bash
mdk init faq-agent -t faq
```

Creates `./faq-agent/` with `agent.yaml`, `prompt.md`, JSON schemas, and a
small eval dataset.

### 2. Run it once

```bash
MOVATE_MOCK_RESPONSE='{"answer": "Hello!", "confidence": 0.95}' \
  mdk run ./faq-agent '{"question": "what is movate?"}' --mock
```

The output JSON shows the validated response, plus `metrics.cost_usd`,
`metrics.latency_ms`, and a `run_id`. Every run lands in
`~/.movate/local.db` (override path with `MOVATE_DB`).

### 3. Eval against the dataset

```bash
mdk eval ./faq-agent --mock --gate 0.7
```

Rich table with per-case scores, mean, pass-rate, total cost, and a
verdict. Exits 1 if the gate fails — wire this into CI to block bad
PRs.

#### Four-dimension scoring (v0.6+)

Every eval run scores up to four dimensions per case:

| Dimension | Always? | Driven by | Scoring |
|---|---|---|---|
| **accuracy** | yes | `expected` | exact-match OR LLM-as-judge (existing v0.5 logic) |
| **faithfulness** | opt-in | `grounding` | LLM judge: "does the answer stay true to this context?" |
| **coverage** | opt-in | `expected_coverage` | deterministic substring match — fraction of topics present |
| **latency** | yes (on success) | `latency_budget_ms` *or* agent `timeouts.call_ms` | 1.0 within budget, linear decay to 0.0 at 2x budget |

Dataset rows opt into the new dims by adding optional fields next to
`input` / `expected`:

```jsonl
{"input": {"text": "warranty?"}, "expected": {"message": "1 year, parts only"},
 "grounding": "Warranty terms: 1 year on parts, no labor coverage.",
 "expected_coverage": ["parts", "1 year"],
 "latency_budget_ms": 1500}
```

The CLI surfaces the per-dim rollup as a small `Dimensional breakdown`
table beneath the main eval output — but **only when** at least one
case opted in to faithfulness or coverage. Legacy datasets see the
same v0.5 output, byte-for-byte.

**Back-compat for the gate**: `--gate 0.7` still means "70% accuracy
across cases". The other three dims are reporting-only. A future flag
will add `--gate-faithfulness` / `--gate-coverage` for per-dim CI gates;
until then the gate keeps the v0.5 contract intact.

The JSON output (`--output json`) carries the rollup at the top level
plus per-run dim scores under each case:

```json
{
  "dimensional_means": {
    "accuracy": 1.0,
    "faithfulness": 0.92,
    "coverage": 0.83,
    "latency": 0.7
  },
  "cases": [{
    "dimensions_per_run": [{
      "accuracy":    {"value": 1.0, "rationale": "exact match"},
      "faithfulness":{"value": 0.9, "rationale": "minor unsupported claim"},
      "coverage":    {"value": 1.0, "rationale": "all topics covered"},
      "latency":     {"value": 0.5, "rationale": "over budget by 750ms"}
    }]
  }]
}
```

### 4. Capture a baseline; detect regressions

```bash
# On main, freeze the current scores as a JSON baseline:
mdk eval ./faq-agent --mock --output-baseline .movate/faq-agent/baseline.json

# In a PR, fail the build if scores drop past the tolerance:
mdk eval ./faq-agent --mock \
  --baseline-file .movate/faq-agent/baseline.json \
  --regression-tolerance 0.05
```

Full CI workflow at
[.github/workflows/eval-gate.example.yml](.github/workflows/eval-gate.example.yml).
Walkthrough: [docs/ci-eval-gate.md](docs/ci-eval-gate.md).

### 5. Debug a regression with replay

```bash
# A specific run looked wrong? Re-execute the *same input* through
# whatever's on disk now (prompt edits, model swaps, schema changes):
mdk run ./faq-agent --replay <run-id> --mock
```

JSON output diffs recorded vs current, surfaces `output_changed`,
`status_changed`, `changed_keys`, and cost/latency deltas.

For a full timeline of an agent or workflow:

```bash
mdk trace replay <run-id-or-workflow-run-id>
```

## Quickstart — service mode (v0.5)

Run MDK as a real service: HTTP runtime + worker pool, sqlite or
Postgres-backed.

### Sqlite (zero infra)

```bash
# Terminal 1: scaffold an agent + start the HTTP runtime
mdk init alpha --target ./agents
mdk serve --port 8000 --agents-path ./agents

# Terminal 2: run a worker (mock mode = no API keys)
MOVATE_MOCK_RESPONSE='{"message":"hi"}' mdk worker --mock

# Terminal 3: mint a key + queue a job
KEY=$(mdk auth create-key --tenant-id "$(uuidgen | tr -d -)" --env live --quiet 2>&1 \
        | grep -o 'mvt_[a-zA-Z0-9_-]*' | head -1)

curl -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"kind":"agent","target":"alpha","input":{"text":"hello"}}' \
  http://127.0.0.1:8000/run
# → {"job_id": "...", "status": "queued"}

# Poll until terminal:
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/jobs/<job_id>
# → {"status": "success", "result_run_id": "...", ...}
```

### Postgres (production)

Same commands; just point `MOVATE_DB_URL` at a Postgres instance:

```bash
export MOVATE_DB_URL="postgresql://user:pw@host:5432/movate"
mdk serve --port 8000 --agents-path ./agents
mdk worker  # in another process; multiple workers run in parallel via SKIP LOCKED
```

API key + job + run state all land in Postgres. JSONB columns are
queryable directly:

```sql
SELECT job_id, status, output->>'message' FROM runs WHERE agent = 'alpha';
```

## Quickstart — submit jobs to a deployed runtime

Once you have a runtime running (locally via `mdk serve` + `mdk worker`, or remotely on Azure), submit jobs from any machine without
hand-crafting `curl` calls.

```bash
# One-time: register a target. The bearer token lives in an env var,
# NOT in the config file.
export MOVATE_PROD_KEY=mvt_live_...   # from `mdk auth create-key`
mdk config add-target prod \
    --url https://movate-prod-api.eastus2.azurecontainerapps.io \
    --key-env MOVATE_PROD_KEY \
    --set-active

# Fire-and-forget — prints {job_id, status} on stdout (pipe-friendly).
mdk submit faq-agent '{"question": "what is movate?"}'

# Wait for completion + desktop notification when done.
# Use this for long evals / bench runs — walk away, come back to a chime.
mdk submit faq-agent '{"question": "..."}' --wait --notify

# Inspect a previously-submitted job.
mdk jobs show <job-id>
mdk jobs wait <job-id> --timeout 600    # block until terminal

# What can this runtime run?
mdk jobs list-agents

# Switch targets mid-session.
mdk submit faq-agent '{...}' --target staging
mdk config use staging  # or set a new default
```

The `--notify` desktop fallback uses `terminal-notifier` / `osascript`
on macOS, `notify-send` on Linux, and is a no-op on Windows. Server-side
SMS / email notifications (per-job `notify_target`, fired by the worker)
are tracked in [BACKLOG.md](BACKLOG.md) for post-v1.0.

## Quickstart — Microsoft Teams bot (v0.7 alpha)

Run Movate agents from inside a Teams channel — no CLI needed for the
end user. Slices 3.1.a + 3.1.b together ship the full demo loop: the
bot parses `@movate run <agent> <json>`, submits to the deployed
runtime, polls until terminal, and renders the result as an Adaptive
Card with cost, latency, and an optional trace link. See
[ADR 003](docs/adr/003-teams-integration.md) for the full design.

```bash
# Install the optional extra (FastAPI + uvicorn).
uv add 'movate-cli[teams]'

# Terminal 1 — Movate runtime:
mdk serve --agents-path ./agents --port 8000

# Terminal 2 — mint an API key for the bot (one-time):
mdk auth create-key --tenant local --name teams-bot
# copy the printed key

# Terminal 3 — Teams bot pointed at the runtime:
MOVATE_TEAMS_FLEET_API_KEY=mvt_dev_local_... \
  mdk teams-bot serve --runtime-url http://127.0.0.1:8000

# Terminal 4 — point the Bot Framework Emulator at:
#   http://localhost:3978/api/messages
# No app id needed for local dev.
```

Then in the Emulator chat:

```
@movate ping             → pong (text reply)
@movate help             → list of commands (text reply)
@movate run faq-agent {"question": "what is movate?"}
                         → submits the run, polls to terminal, renders
                           an Adaptive Card with the response, cost,
                           latency, and an optional Langfuse trace link
```

Five outcome variants render as four distinct cards: success, terminal
failure, timeout (with `mdk jobs show <id>` recovery hint), and client
failure (with category-specific hints like "set MOVATE_TEAMS_FLEET_API_KEY").

The bot is a thin client of the existing v0.5 HTTP runtime — same
`/run`, `/eval`, `/jobs` endpoints `mdk` itself talks to. Per-user
auth (`/movate connect` DM flow), file uploads (drag agent.yaml +
dataset.jsonl into a channel), and the Teams manifest land in
follow-up slices (3.1.d, 3.1.e).

### Per-user identity binding (slice 3.1.c)

Each Teams user can bind their own Movate API key in a DM, so every
`@movate run` they trigger lands in `RunRecord.created_by` correctly.
Keys are encrypted at rest using Fernet (`cryptography` package);
the bot reads the encryption key from `MOVATE_TEAMS_ENCRYPTION_KEY`.

```bash
# Mint a fresh encryption key (one-time, per bot deployment):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Export it before booting the bot:
export MOVATE_TEAMS_ENCRYPTION_KEY=<paste-key>
```

Then in a DM with the bot:

```
/movate connect mvt_test_<tenant>_<keyid>_<secret>   → ✓ bound to tenant <X>
/movate whoami                                       → shows tenant + last 4 chars
/movate disconnect                                   → removes the binding
```

Identity commands are **DM-only** — channel posts are rejected with a
"DM me" card so API keys don't leak into team channels.

| Mode | Behavior |
|---|---|
| Default (`--no-identity` off, `--require-binding` off) | Bot accepts bindings; unbound users fall back to the fleet key |
| Strict (`--require-binding`) | Bot rejects `run` from unbound users — use for multi-tenant deployments where every run must be attributable |
| `--no-identity` | Disables binding entirely; every user uses the fleet key. Smoke-test mode |

### Optional config

| Env var | Effect |
|---|---|
| `MOVATE_TEAMS_FLEET_API_KEY` | Bot's API key for the runtime. Required for `run` to work (or per-user keys via `/connect`). |
| `MOVATE_TEAMS_ENCRYPTION_KEY` | **Required** for identity binding. 32-byte url-safe-base64 Fernet key. |
| `MOVATE_RUNTIME_URL` | Default runtime URL (overridden by `--runtime-url`). |
| `MOVATE_TEAMS_DB` | Where to store the bot's `teams_users` sqlite db. Defaults to `~/.movate/teams.db`. |
| `MOVATE_TEAMS_REQUIRE_BINDING` | `1` enables strict mode (same as `--require-binding`). |
| `MOVATE_TEAMS_LANGFUSE_PUBLIC_HOST` | When set, success cards include a "View trace" button. Off by default — don't show prospects an internal URL. |
| `MOVATE_TEAMS_RUN_TIMEOUT_S` | Max seconds to wait for a job before returning a timeout card. Defaults to 25 (under Teams' channel timeout). |

## Quickstart — deploy to Azure Container Apps

Once the Bicep IaC has provisioned a resource group, ACR, Container Apps
environment, and Postgres ([infra/azure/README.md](infra/azure/README.md)
walks the first-time setup), shipping a code change is one command:

```bash
# One-time: register the deploy target with its Azure metadata. The
# bearer token still lives in an env var; --azure-* fields tell
# `mdk deploy` where to push images and which apps to update.
mdk config add-target prod \
    --url https://movate-prod-api.eastus2.azurecontainerapps.io \
    --key-env MOVATE_PROD_KEY \
    --azure-subscription "$SUBSCRIPTION_ID" \
    --azure-resource-group movate-prod-rg \
    --azure-acr movateprodacr \
    --azure-env prod \
    --set-active

# Build the image in ACR (no local Docker needed) + roll out both
# Container Apps + poll /healthz until version matches. Default tag is
# movate:<version>-<git-sha-short>.
mdk deploy --target prod

# CI / fire-and-forget — skip the /healthz verification step.
mdk deploy --target prod --no-wait

# Rollback to a previously-built image (no rebuild).
mdk deploy --target prod --skip-build --image-tag movate:0.5.0-abc1234

# Worker-only update (e.g. dispatch-logic change).
mdk deploy --target prod --only worker

# Plan inspection — prints the `az` commands without running them.
mdk deploy --target prod --dry-run
```

For CI, push a commit to a `release/<env>` branch (e.g. `release/prod`)
and [.github/workflows/deploy.yml](.github/workflows/deploy.yml) runs
the same `mdk deploy` flow with Azure federated OIDC auth — no
client secrets stored in GitHub. Per-env GitHub *Environments* hold
the scoped secrets so prod can require approval gates.

## policy.yaml — project-wide defaults

Set values once at the project level and every `agent.yaml` inherits
them — without copy-pasting `temperature: 0.0` into every file.

```yaml
# policy.yaml
defaults:
  model:
    params:
      temperature: 0.0
      max_tokens: 1024
  timeouts:
    call_ms: 15000
  budget:
    max_cost_usd_per_run: 0.50
```

**Agent.yaml always wins per-key.** Defaults only fill what the agent
didn't specify:

* `model.params` merges per-key (default `temperature: 0.0` applies
  only to agents that don't write their own `temperature`).
* `timeouts.call_ms` / `timeouts.total_ms` are per-field.
* `budget.max_cost_usd_per_run` is per-field.

Distinct from `policy:` (the enforced ceiling — agents can't exceed
it) and `runtime:` (the gate on `AgentRuntime` values). Defaults are
*suggestions* that fill gaps; policy is the *enforced* contract.

See `mdk show <agent>` to inspect the resolved values after defaults
are applied — that's what's actually going to run.

### Canonical config split (v0.6+)

`policy.yaml` doesn't have to hold every project-wide setting anymore.
The loader looks for up to four files at the project root and merges
them; each one carries a focused slice:

| File | Owns |
|---|---|
| `policy.yaml` | `policy:` (enforced rules) + `defaults:` (suggestions) + `agents_dir:` + `workflows_dir:` |
| `runtime.yaml` | `runtime:` (which `AgentRuntime` values are allowed) |
| `eval.yaml` | `eval:` + `bench:` (project-wide eval gate, bench model list) |
| `knowledge.yaml` | `knowledge:` — stub today; reserved for v0.7+ RAG config |

**Migration is incremental.** Cut a block from `policy.yaml`, paste
into its dedicated file. If both files carry the same block,
**dedicated wins** and a one-shot deprecation warning fires on stderr.
Operators stay on the unified `policy.yaml` indefinitely if they want
— the split is opt-in.

```yaml
# runtime.yaml — one block, no nesting
runtime:
  allowed: [litellm]
```

```yaml
# eval.yaml — both blocks live here naturally; they're both about scoring
eval:
  gate: 0.85
bench:
  models:
    - openai/gpt-4o-mini-2024-07-18
    - anthropic/claude-haiku-4-5-20251001
```

`mdk policy export` reads all four files and prints the merged view —
useful for diffing the *effective* config across environments rather
than four files at a time.

### Skill policy — gate `side_effects` categories

Operators can restrict which skill `side_effects` categories agents in
the project are allowed to use. Same enforcement points as model policy
(static at `mdk validate` + runtime at `Executor.execute` entry):

```yaml
# policy.yaml
skills:
  # Only pure-lookup skills allowed. Agents using network /
  # filesystem / mutates-state skills fail `mdk validate`.
  allowed_side_effects: [read-only]
```

The four `side_effects` categories come from `skill.yaml`:

| Category | Meaning |
|---|---|
| `read-only` | Pure computation. No I/O, no state change. (e.g. calculator) |
| `network` | Makes outbound network calls. (e.g. HTTP lookups, web search) |
| `filesystem` | Reads or writes the local filesystem. |
| `mutates-state` | Changes external state. (e.g. send email, create ticket) |

Default is permissive (`allowed_side_effects: None` — every category
accepted). An empty list `[]` is the strictest config: no skills at
all, agents must declare `skills: []` to validate.

## Skills — agents that use tools

An agent can invoke reusable callables ("skills") mid-turn. Declare each
skill in its own folder under `skills/`, reference it by name from
`agent.yaml`, and the executor runs a tool-use loop for you.

```
my-project/
├── policy.yaml
├── agents/calc-agent/agent.yaml         # references the skill by name
└── skills/calculator/
    ├── skill.yaml                       # contract
    └── impl.py                          # Python entrypoint
```

```yaml
# skills/calculator/skill.yaml
api_version: movate/v1
kind: Skill
name: calculator
version: 0.1.0
description: Evaluate simple arithmetic expressions

schema:
  input:
    expression: string
  output:
    result: number

implementation:
  kind: python                      # also: http (PR #54), mcp (later)
  entry: skills.calculator.impl:evaluate

cost:
  per_call_usd: 0.0001              # added to RunRecord.metrics.cost_usd
```

```python
# skills/calculator/impl.py
def evaluate(input, ctx):
    # Sync OR async function — both work. `ctx` carries trace_id,
    # tenant_id, run_id, and the effective timeout in ms.
    return {"result": eval(input["expression"], {"__builtins__": {}})}
```

```yaml
# agents/calc-agent/agent.yaml
skills:
  - calculator
```

The executor handles the rest: converts each skill's schema into a
tool spec for the model, dispatches `tool_use` responses to the
matching backend, validates the output against the skill's output
schema, and feeds the result back as a `tool_result` until the
model emits a final answer. Hard cap of 10 tool turns guards against
runaway loops.

Five error types map cleanly to whatever can go wrong — visible to
the model in `tool_result` blocks and to operators in run traces:

| `type` | When it fires |
|---|---|
| `not_found` | Model invented a tool name not in the registry |
| `validation_failed` | Input or output didn't match the skill's schema |
| `backend_error` | Function raised, HTTP returned non-2xx, etc. |
| `timeout` | Skill exceeded its `timeout_call_ms` budget |
| `budget_exceeded` | Per-run cost cap hit during the loop |

`mdk show ./skills/calculator` renders the resolved spec.
`mdk show ./agents/calc-agent` lists the agent's skills inline.

### Tool-use across runtimes

Skills work transparently across every runtime — the executor owns the
loop, providers translate the wire format. Same `skills:` declaration,
same dispatch, same five-error taxonomy:

| Runtime | Tool-spec shape | Notes |
|---|---|---|
| `litellm` (default) | OpenAI-style nested `{type: "function", function: {...}}` | LiteLLM forwards to the upstream provider unchanged. |
| `native_anthropic` | Flat `{name, description, input_schema}` | Native `tool_use` content blocks; preserves reasoning text before the tool call. v0.6+ |
| `native_openai` | OpenAI-style nested (same as litellm) | Direct SDK call — bypasses LiteLLM's normalisation. v0.6+ |
| `langchain` | Inherits from the wrapped Runnable | Tool-use defers to whatever the LangChain pipeline does. |

For native_anthropic specifically, the adapter translates the executor's
OpenAI-style message history (`role="assistant"` with `tool_calls` +
`role="tool"` results) into Anthropic's content-block format
(`tool_use` blocks on assistant messages, `tool_result` blocks on user
messages, with consecutive tool_results coalesced into a single user
message). The executor doesn't see Anthropic's wire format — it's an
implementation detail of the provider.

```yaml
# Same agent, two ways to run it — output is identical.
runtime: litellm
model:
  provider: anthropic/claude-sonnet-4-6  # LiteLLM-prefixed
---
runtime: native_anthropic
model:
  provider: claude-sonnet-4-6            # bare model id; pricing.yaml lookup auto-prefixes
```

### HTTP skills — call any REST API as a tool

For skills that hit an external service (CRM, warranty system, weather,
hosted ML endpoint), use `implementation.kind: http` — no Python wrapper
needed.

```yaml
# skills/warranty-lookup/skill.yaml
api_version: movate/v1
kind: Skill
name: warranty-lookup
version: 0.1.0
description: Fetch warranty status for a customer case.

schema:
  input:
    case_id: string
  output:
    status: pending|active|expired|unknown
    expires_at: string?

implementation:
  kind: http
  entry: https://crm.internal.movate.com/api/warranty/{{ input.case_id }}
  method: GET                           # default POST; pick what your API expects
  auth: bearer-from-env:CRM_TOKEN       # Authorization: Bearer $CRM_TOKEN
  headers:
    X-API-Version: "2026-01"
  timeout_seconds: 10                   # optional; falls through to call_ms otherwise

cost:
  per_call_usd: 0.0                     # free internal API
```

The URL may contain `{{ input.* }}` Jinja placeholders rendered against
the call's input dict. POST/PUT/PATCH skills send the full input as the
JSON body; GET/DELETE send it as query parameters.

The backend handles the failure modes you'd expect:

* Missing auth env var → `backend_error` ("env var CRM_TOKEN is unset")
* Non-2xx response → `backend_error` with status + body excerpt
* Non-JSON or non-object response → `backend_error` / `validation_failed`
* Transport errors / timeouts → `timeout` / `backend_error`

Only `bearer-from-env:VAR` auth is supported today; basic-auth and
arbitrary-header forms land in a follow-up PR.

### MCP skills — plug in a Model Context Protocol server

For skills that live behind an MCP server (Anthropic's tool-server
protocol), use `implementation.kind: mcp`. Operators can wire up
internal tool servers, npx-installed community servers, or
customer-hosted bridges to legacy systems without writing Python.

```yaml
# skills/github-issue/skill.yaml
api_version: movate/v1
kind: Skill
name: github-issue
version: 0.1.0
description: Fetch a GitHub issue via the github-mcp bridge.

schema:
  input:
    repo: string
    issue_number: integer
  output:
    title: string
    body: string

implementation:
  kind: mcp
  entry: npx -y @movate/github-mcp        # command to spawn the MCP server
  tool: get_issue                         # which tool on the server to call
```

The backend spawns the server subprocess on first use, performs the
MCP handshake (`initialize` → `notifications/initialized`), and
reuses the running process across every subsequent tool call to the
same server. Multiple skills pointing at the same `entry` command
share one subprocess.

Two response shapes are supported:

* **`structuredContent`** (modern MCP servers) — used directly as
  the skill's output.
* **`content[0].text`** parsed as JSON — fallback for older servers
  that haven't adopted the structured form.

Failure modes map to the same `SkillError` taxonomy as Python + HTTP:

* Subprocess fails to start / dies mid-call → `backend_error`
* Tool name not in the server's `tools/list` → `backend_error` with
  the available list
* Server reports `isError: true` or returns a JSON-RPC error
  envelope → `backend_error` with the server's message
* Response content isn't a JSON object → `validation_failed`
* Server hangs past the budget → `timeout`

The implementation is hand-rolled (no external `mcp` SDK dep) — the
protocol surface we use is small enough that a focused client is
cleaner than adding a library. HTTP/SSE transport for remote MCP
servers lands in a follow-up if real customer demand surfaces.

### Operator commands

```bash
# Drop a starter skill from the packaged template
mdk skills scaffold calculator

# See every skill the project's `skills/` folder exposes
mdk skills list

# Test a skill in isolation without wiring it into an agent
mdk skills run calculator '{"query": "what is 41+1?"}'
```

`mdk skills run` validates the input against the skill's schema,
dispatches via the registered backend, validates the output, and
prints the result on stdout. Identical code path to what the
executor runs per tool call inside a real agent — but without the
LLM cost. Great for iterating on `impl.py` (or the HTTP API behind
an http skill) before committing to a full agent run.

See [docs/adr/002-skills-and-contexts.md](docs/adr/002-skills-and-contexts.md)
for the full design.

## Contexts — shared prompt fragments

Stop copy-pasting the company style guide into every `prompt.md`. Drop
markdown files in `contexts/` at the project root, reference them by
name from each agent, and the loader prepends them to the rendered
prompt — in declaration order, joined by a horizontal-rule separator.

```
my-project/
├── agents/faq-agent/agent.yaml
└── contexts/
    ├── style-guide.md
    ├── glossary.md
    └── safety-disclaimer.md
```

```yaml
# agents/faq-agent/agent.yaml
contexts:
  - style-guide
  - glossary
```

The rendered prompt becomes:

```
<style-guide.md body>

---

<glossary.md body>

---

<your agent's prompt.md, with Jinja still applied to {{ input.* }}>
```

**Pure markdown — no Jinja, no Python, no template syntax.** Contexts
are documentary. Need dynamic prefix logic? Wrap it in a skill instead
(the skill returns a string the model uses).

Constraints (deliberate):

- Flat layout only — `contexts/<name>.md`, no nested subfolders.
- Filename basename (minus `.md`) is the reference name.
- Stray `README.txt`, `.DS_Store`, etc. are silently ignored.

`mdk show ./agents/faq-agent` lists the agent's contexts with per-file
byte sizes so operators can spot a runaway file inflating prompt cost.

## agent.yaml — schema shorthand

For agents with a handful of fields the `schema/` subfolder is overkill.
Declare the contract inline:

```yaml
# agent.yaml
schema:
  input:
    message: string
    priority: integer?         # ? suffix = optional
  output:
    response: string
    sentiment: positive|negative|neutral   # | = string enum
    tags: [string]             # [T] = array of T
```

The loader compiles this into JSON Schema at load time —
`additionalProperties: false`, every non-`?` field is required. For
complex contracts (refs, `oneOf`, regex), keep using the path form
pointing at a full JSON Schema file:

```yaml
schema:
  input: ./schema/input.json
  output: ./schema/output.json
```

Both forms coexist; pick per-agent. The shorthand only describes
strict object schemas — anything else uses the path form.

## Available templates

| `-t` value | Shape | Eval default |
|---|---|---|
| `default` | Minimal echo agent (string-in, string-out) | exact-match |
| `faq` | Question → answer + confidence | ships `judge.yaml.example` |
| `summarizer` | Text + max_words → summary + word_count | ships `judge.yaml.example` |
| `classifier` | Text + label list → chosen label | exact-match (finite labels) |

## CLI shape

```
Develop          init, validate, show
Run & evaluate   run, bench, eval, logs, trace
Diagnose         doctor, pricing
Deploy & operate serve, worker, deploy
Manage           auth
```

`mdk doctor` reports environment status, configured providers, the
local DB path, and which optional extras are installed (`langfuse`,
`otel`, `runtime`).

## Configuration

Environment variables MDK reads:

| var | purpose | default |
|---|---|---|
| `MOVATE_DB` | SQLite path | `~/.movate/local.db` |
| `MOVATE_MOCK_RESPONSE` | What `MockProvider` returns | `'{"reply": "ok"}'` |
| `MOVATE_TRACER` | Force a tracer (`stdout` / `langfuse` / `otel` / `composite`) | auto-detect from other env |
| `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY` | Langfuse auth | unset |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP-HTTP target | unset |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / etc. | Provider auth (LiteLLM passthrough) | unset |

### Model policy

Org-wide guardrails on which providers / models / cost ceilings an agent
may use. Declared in `movate.yaml`:

```yaml
policy:
  allowed_providers: [openai, azure, anthropic]   # provider prefixes
  deny_models:
    - openai/gpt-3.5-turbo                        # explicit blocklist
  max_cost_per_run_usd: 0.50                      # caps agent budget
```

Enforced at two layers:

* **`mdk validate <agent>`** — static check on every `agent.yaml`
  before merge. Reports all violations (primary model, every fallback,
  budget ceiling) in one pass and exits 2.
* **`Executor.execute()`** entry — runtime check at every invocation,
  so bundles loaded over HTTP by `mdk serve` can't bypass the
  static gate. Denied models short-circuit before any provider call —
  zero cost incurred for a forbidden run.

All three fields are optional; an absent or empty `policy:` block is the
permissive default (no restrictions). The policy can only tighten —
the runtime ceiling is `min(agent.budget.max_cost_usd_per_run, policy)`
so an agent's authored budget can never relax the org cap.

## Development

```bash
uv sync --all-extras --dev
uv run ruff format src tests
uv run ruff check src tests
uv run mypy src              # strict
uv run pytest -m unit        # ~370 tests, ~20s
uv run pytest -m smoke       # opt-in real-API smoke; needs keys
```

Architecture decisions and roadmap docs:

- [PRD_starting.md](PRD_starting.md) — full product vision
- [docs/v0.5-design.md](docs/v0.5-design.md) — service-mode design lock-in
- [docs/v0.3-langgraph-prototype.md](docs/v0.3-langgraph-prototype.md) — IR/LangGraph compatibility findings
- [docs/ci-eval-gate.md](docs/ci-eval-gate.md) — CI integration guide
- [BACKLOG.md](BACKLOG.md) — prioritized work list
- [CHANGELOG.md](CHANGELOG.md) — release notes
- [RELEASING.md](RELEASING.md) — private-distribution paths

## Testing your own agent

Add to your project's `conftest.py`:

```python
pytest_plugins = ["movate.testing.fixtures"]
```

Then your tests can use the bundled doubles + fixtures:

```python
from movate.core.loader import load_agent
from movate.core.models import RunRequest

async def test_my_agent(temp_agent_dir, build_executor):
    bundle = load_agent(temp_agent_dir)
    executor, _, storage, tracer = build_executor(response='{"message": "ok"}')
    response = await executor.execute(
        bundle, RunRequest(agent=bundle.spec.name, input={"text": "hi"})
    )
    assert response.status == "success"
    assert len(storage.runs) == 1
```

Full surface: [src/movate/testing/](src/movate/testing/) — `MockProvider`,
`JudgeStubProvider`, `InMemoryStorage`, `NullTracer`, `scaffold_agent`,
`build_test_executor`.

## Live-API smoke (opt-in)

Real money. Skipped by default. Run before tagging a release:

```bash
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-...
bash scripts/smoke.sh    # auto-sets MOVATE_SMOKE=1
```

Each test is independently gated on the relevant API key, so a partial
keyring still produces a useful result.

## License

Proprietary. Internal Movate use only.
