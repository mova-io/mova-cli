# movate

Declarative platform for building, evaluating, and deploying AI agents and workflows.

**Internal Movate framework.** Proprietary; private artifact distribution
only — see [RELEASING.md](RELEASING.md). Public PyPI is intentionally
not used.

## Status

| version | tag | what landed |
|---|---|---|
| 0.5.0 | [`v0.5.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.5.0) | HTTP runtime + worker + Postgres — movate is now a service |
| 0.4.0 | [`v0.4.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.4.0) | Observability + regression-detection (Langfuse, OTel, trace replay, eval baseline diff, run replay, CI eval-gate) |
| 0.3.1 | [`v0.3.1`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.1) | Workflow runner double-save fix |
| 0.3.0 | [`v0.3.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.0) | Sequential workflows (forward-aware IR + compiler + runner) |
| 0.2.0 | [`v0.2.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.2.0) | Eval engine (exact-match + LLM-as-judge with cross-family enforcement) |

**v1.0 next (`main`)** — Azure deploy + production hardening: Bicep IaC
(ACA + Postgres Flex + ACR + Key Vault), `movate deploy`, model policy
enforcement, tenant isolation audit. See
[docs/v0.5-design.md](docs/v0.5-design.md) for the v0.5 architecture
that v1.0 builds on.

## What works today

| capability | command | status |
|---|---|---|
| Scaffold an agent | `movate init <name> -t <template>` | ✓ v0.1 |
| Validate agent.yaml + schemas | `movate validate <path>` | ✓ v0.1 |
| Run an agent locally | `movate run <path> <input> [--mock]` | ✓ v0.1 |
| Per-provider pricing introspection | `movate pricing` | ✓ v0.1 |
| Multi-model bench | `movate bench <path>` | ✓ v0.2 |
| Eval suite + gating | `movate eval <path> --gate 0.7` | ✓ v0.2 |
| Sequential workflow execution | `movate run <workflow-path>` | ✓ v0.3 |
| Trace replay (agent + workflow) | `movate trace replay <id>` | ✓ v0.4 |
| Regression detection vs baseline | `movate eval --baseline <id>` <br> `movate eval --baseline-file <path>` | ✓ v0.4 |
| Re-run a stored input against current code | `movate run <path> --replay <run-id>` | ✓ v0.4 |
| API key issuance / revocation | `movate auth create-key | list-keys | revoke-key` | ✓ v0.5 |
| HTTP runtime | `movate serve` | ✓ v0.5 |
| Background worker | `movate worker` | ✓ v0.5 |
| Postgres backend | `MOVATE_DB_URL=postgresql://...` | ✓ v0.5 |
| Submit jobs to a deployed runtime | `movate submit <agent>` (with `--wait` + `--notify`) | ✓ v0.5+ |
| Inspect jobs on a deployed runtime | `movate jobs show | wait | list-agents` | ✓ v0.5+ |
| Manage deployment targets | `movate config add-target | use | list-targets` | ✓ v0.5+ |
| Azure deploy (Bicep IaC) | `infra/azure/main.bicep` (manual `az deployment`) | ✓ v1.0 stage 1 |
| One-command deploy to ACA | `movate deploy --target <name>` | ✓ v1.0 stage 2 |
| Auto-deploy on push to release/* | `.github/workflows/deploy.yml` (federated OIDC) | ✓ v1.0 stage 2 |
| Azure preflight diagnostic | `movate doctor --target <name>` | ✓ v1.0 |
| Azure bootstrap (RG + SP + federated cred) | `scripts/azure-bootstrap.sh <env>` | ✓ v1.0 |
| Job retry + dead-letter on transient failures | `JobRetryPolicy` + `movate jobs list --status dead_letter` | ✓ post-v1.0 |
| Liveness + readiness probes for ACA | `GET /healthz` (cheap) + `GET /ready` (deep checks) | ✓ post-v1.0 |
| Per-API-key rate limiting | `movate serve --rate-limit-per-minute 60` | ✓ post-v1.0 |
| Worker autoscaling on queue depth | KEDA postgresql scaler in `containerapp-worker.bicep` | ✓ post-v1.0 |
| Per-tenant monthly cost ceiling | `movate tenants set-budget <id> --monthly-usd 500` | ✓ post-v1.0 |
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
movate init faq-agent -t faq
```

Creates `./faq-agent/` with `agent.yaml`, `prompt.md`, JSON schemas, and a
small eval dataset.

### 2. Run it once

```bash
MOVATE_MOCK_RESPONSE='{"answer": "Hello!", "confidence": 0.95}' \
  movate run ./faq-agent '{"question": "what is movate?"}' --mock
```

The output JSON shows the validated response, plus `metrics.cost_usd`,
`metrics.latency_ms`, and a `run_id`. Every run lands in
`~/.movate/local.db` (override path with `MOVATE_DB`).

### 3. Eval against the dataset

```bash
movate eval ./faq-agent --mock --gate 0.7
```

Rich table with per-case scores, mean, pass-rate, total cost, and a
verdict. Exits 1 if the gate fails — wire this into CI to block bad
PRs.

### 4. Capture a baseline; detect regressions

```bash
# On main, freeze the current scores as a JSON baseline:
movate eval ./faq-agent --mock --output-baseline .movate/faq-agent/baseline.json

# In a PR, fail the build if scores drop past the tolerance:
movate eval ./faq-agent --mock \
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
movate run ./faq-agent --replay <run-id> --mock
```

JSON output diffs recorded vs current, surfaces `output_changed`,
`status_changed`, `changed_keys`, and cost/latency deltas.

For a full timeline of an agent or workflow:

```bash
movate trace replay <run-id-or-workflow-run-id>
```

## Quickstart — service mode (v0.5)

Run movate as a real service: HTTP runtime + worker pool, sqlite or
Postgres-backed.

### Sqlite (zero infra)

```bash
# Terminal 1: scaffold an agent + start the HTTP runtime
movate init alpha --target ./agents
movate serve --port 8000 --agents-path ./agents

# Terminal 2: run a worker (mock mode = no API keys)
MOVATE_MOCK_RESPONSE='{"message":"hi"}' movate worker --mock

# Terminal 3: mint a key + queue a job
KEY=$(movate auth create-key --tenant-id "$(uuidgen | tr -d -)" --env live --quiet 2>&1 \
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
movate serve --port 8000 --agents-path ./agents
movate worker  # in another process; multiple workers run in parallel via SKIP LOCKED
```

API key + job + run state all land in Postgres. JSONB columns are
queryable directly:

```sql
SELECT job_id, status, output->>'message' FROM runs WHERE agent = 'alpha';
```

## Quickstart — submit jobs to a deployed runtime

Once you have a runtime running (locally via `movate serve` + `movate
worker`, or remotely on Azure), submit jobs from any machine without
hand-crafting `curl` calls.

```bash
# One-time: register a target. The bearer token lives in an env var,
# NOT in the config file.
export MOVATE_PROD_KEY=mvt_live_...   # from `movate auth create-key`
movate config add-target prod \
    --url https://movate-prod-api.eastus2.azurecontainerapps.io \
    --key-env MOVATE_PROD_KEY \
    --set-active

# Fire-and-forget — prints {job_id, status} on stdout (pipe-friendly).
movate submit faq-agent '{"question": "what is movate?"}'

# Wait for completion + desktop notification when done.
# Use this for long evals / bench runs — walk away, come back to a chime.
movate submit faq-agent '{"question": "..."}' --wait --notify

# Inspect a previously-submitted job.
movate jobs show <job-id>
movate jobs wait <job-id> --timeout 600    # block until terminal

# What can this runtime run?
movate jobs list-agents

# Switch targets mid-session.
movate submit faq-agent '{...}' --target staging
movate config use staging  # or set a new default
```

The `--notify` desktop fallback uses `terminal-notifier` / `osascript`
on macOS, `notify-send` on Linux, and is a no-op on Windows. Server-side
SMS / email notifications (per-job `notify_target`, fired by the worker)
are tracked in [BACKLOG.md](BACKLOG.md) for post-v1.0.

## Quickstart — deploy to Azure Container Apps

Once the Bicep IaC has provisioned a resource group, ACR, Container Apps
environment, and Postgres ([infra/azure/README.md](infra/azure/README.md)
walks the first-time setup), shipping a code change is one command:

```bash
# One-time: register the deploy target with its Azure metadata. The
# bearer token still lives in an env var; --azure-* fields tell
# `movate deploy` where to push images and which apps to update.
movate config add-target prod \
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
movate deploy --target prod

# CI / fire-and-forget — skip the /healthz verification step.
movate deploy --target prod --no-wait

# Rollback to a previously-built image (no rebuild).
movate deploy --target prod --skip-build --image-tag movate:0.5.0-abc1234

# Worker-only update (e.g. dispatch-logic change).
movate deploy --target prod --only worker

# Plan inspection — prints the `az` commands without running them.
movate deploy --target prod --dry-run
```

For CI, push a commit to a `release/<env>` branch (e.g. `release/prod`)
and [.github/workflows/deploy.yml](.github/workflows/deploy.yml) runs
the same `movate deploy` flow with Azure federated OIDC auth — no
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

`movate doctor` reports environment status, configured providers, the
local DB path, and which optional extras are installed (`langfuse`,
`otel`, `runtime`).

## Configuration

Environment variables movate reads:

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

* **`movate validate <agent>`** — static check on every `agent.yaml`
  before merge. Reports all violations (primary model, every fallback,
  budget ceiling) in one pass and exits 2.
* **`Executor.execute()`** entry — runtime check at every invocation,
  so bundles loaded over HTTP by `movate serve` can't bypass the
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
