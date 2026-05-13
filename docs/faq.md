# MDK FAQ — basic, medium, advanced

**Audience:** Movate engineers + sales engineers + customer-engagement architects who need to talk about MDK without re-reading every ADR.

**How to use this:**

- **Sales / customer-facing**: skim §1 (Basic). Most prospect questions are answered there. Forward §1 to prospects directly if useful.
- **New engineer onboarding**: §1 then §2. After §2 you can read the codebase without getting lost.
- **Architecture discussions**: §3. Same content the ADRs cover, condensed into Q&A.

If a question isn't here and feels common enough to add, open a PR — these answers compound.

---

## Table of contents

- [§1 — Basic (what is MDK?)](#1--basic-what-is-mdk)
- [§2 — Medium (how does MDK work?)](#2--medium-how-does-mdk-work)
- [§3 — Advanced (architecture + extension)](#3--advanced-architecture--extension)
- [§4 — Operations](#4--operations)
- [§5 — Eval + quality](#5--eval--quality)
- [§6 — Security + multi-tenancy](#6--security--multi-tenancy)
- [§7 — Roadmap + comparison](#7--roadmap--comparison)

---

## §1 — Basic (what is MDK?)

### What is MDK?

MDK (Movate Development Kit) is Movate's **declarative platform for
building, evaluating, and deploying AI agents**. Engineers write an
`agent.yaml` describing what an agent does (prompt, model, input/output
schemas, evaluation cases); MDK handles everything else — running it,
testing it, deploying it, observing it, governing it.

The CLI is installed as `mdk` (or its transitional alias `movate`).
Same binary; either name works.

### What problem does it solve?

The gap between "I have a prompt that works in a notebook" and "I have
an agent that I can hand to a colleague, deploy to production, evaluate
against a dataset, and trust in front of a customer."

Specifically:

- **Reproducibility** — same agent.yaml produces the same run today,
  next month, on another machine. Every run is recorded with the
  prompt hash, model id, pricing version.
- **Eval + regression detection** — score against a dataset; fail the
  build if a PR regresses a previously-passing case.
- **Observability** — every run produces a trace; Langfuse + OpenTelemetry
  catch the prompt/response/cost/latency.
- **Multi-provider** — switch from OpenAI to Anthropic by editing one
  line; no code change.
- **Deployment** — `mdk deploy` ships the same agent to Azure Container
  Apps. Customer can swap to k8s / EKS / GKE with the Helm chart.

### Who built it and why?

Built internally by Movate engineering. The PRD lists three motivations:

1. Customer engagements were rebuilding the same agent infrastructure
   (prompt management, eval harness, observability, deploy pipeline)
   from scratch every time. MDK is that infrastructure, once.
2. Customers wanted a single artifact (`agent.yaml`) to review + approve,
   not a Python notebook to audit line-by-line.
3. Movate wanted to differentiate on **production-grade agent shipping**,
   not just demos. MDK is the production-grade part.

### Is MDK open source?

**No.** MDK is Movate proprietary IP. The framework + CLI live in a
private repo; customer engagements get builds + access via the engagement
contract. Movate sells MDK-built agent systems, not MDK itself.

**However** — every dependency MDK uses is permissively licensed (MIT,
Apache 2.0, BSD, PostgreSQL License). See [`docs/license-posture.md`](license-posture.md)
and [`docs/stack-defense.md`](stack-defense.md). A customer-VPC deploy of
an MDK-built agent contains only permissive OSS + Movate's framework
code; no copyleft contamination, no AGPL service-side obligations.

### What is an "agent" in MDK?

A directory with an `agent.yaml` declaration, a `prompt.md` template,
and JSON schemas for input + output. Optionally: skills (callable
tools), contexts (shared prompt fragments), eval datasets, objectives
with thresholds. The minimum shape:

```
faq-agent/
├── agent.yaml           # declaration
├── prompt.md            # Jinja2 template
├── schema/
│   ├── input.json       # what the agent accepts
│   └── output.json      # what the agent must return
└── evals/
    └── dataset.jsonl    # test cases
```

`mdk init faq-agent -t faq` scaffolds this in <5 seconds.

### What's the smallest useful MDK command sequence?

```bash
mdk init faq-agent -t faq                                  # scaffold
mdk validate ./faq-agent                                   # check schemas + prompt
mdk run ./faq-agent '{"question": "what is movate?"}'     # one-shot run
mdk eval ./faq-agent --gate 0.7                            # score against dataset
```

That's the inner loop. Five minutes from zero to a tested agent.

### What providers does MDK support?

Out of the box, ~40 model providers via LiteLLM: OpenAI, Anthropic,
Azure OpenAI, Google Vertex / Gemini, AWS Bedrock, Cohere, Mistral,
Groq, Together AI, Fireworks, Ollama (local), and more. Native SDKs
also available for OpenAI + Anthropic (lets agents use features LiteLLM
doesn't expose: prompt caching, native tool-use, structured outputs).

### What does a typical `agent.yaml` look like?

```yaml
api_version: movate/v1
kind: Agent

name: warranty-classifier
version: 0.1.0
description: Classify warranty support tickets into 5 categories

model:
  provider: openai/gpt-4o-mini-2024-07-18
  params:
    temperature: 0.0
    max_tokens: 256
  fallback:
    - provider: anthropic/claude-haiku-4-5-20251001

prompt: ./prompt.md

schema:
  input: ./schema/input.json
  output: ./schema/output.json

evals:
  dataset: ./evals/dataset.jsonl

budget:
  max_cost_usd_per_run: 0.05

objectives:
  - id: routing-accuracy
    description: Tickets land in the right category
    threshold: 0.85
    judge: exact

timeouts:
  call_ms: 30000
  total_ms: 60000
```

This is a complete, runnable agent.

### Why YAML instead of Python?

Three reasons:

1. **Reviewers don't read code as fast as YAML.** A non-engineer (PM,
   compliance reviewer, customer) can read `agent.yaml` and understand
   what's about to happen.
2. **Versioning + diffing.** YAML changes are trivial to diff. Python
   prompt changes can hide in a multi-line string formatter.
3. **Less code to audit.** Every line in the YAML maps to a declarative
   intent. There's no business logic to read between the lines.

The escape hatch: an agent's Python skill (`implementation.kind: python`)
can run arbitrary code. That code is `mdk validate`'d for signature +
policy compliance.

---

## §2 — Medium (how does MDK work?)

### What's the runtime architecture?

```
┌─────────────────────────────────────────────────────────────┐
│  agent.yaml + prompt.md + schemas (operator's repo)         │
└─────────────────────┬───────────────────────────────────────┘
                      │ mdk validate / mdk run / mdk eval
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  Executor (src/movate/core/executor.py)                     │
│  1. Render prompt with input via Jinja2                      │
│  2. Validate against input schema                            │
│  3. Call BaseLLMProvider.complete() (retry + fallback)       │
│  4. Tool-use loop if skills declared                         │
│  5. Validate output against output schema                    │
│  6. Save RunRecord to storage                                │
│  7. Emit OTel + Langfuse spans                               │
└─────────────────────┬───────────────────────────────────────┘
                      │
       ┌──────────────┼──────────────┐
       ▼              ▼              ▼
   Provider       Storage         Tracer
  (LiteLLM /    (SQLite /       (Langfuse,
  Anthropic /   Postgres)        OTel)
  OpenAI)
```

### What's a `RunRecord`?

The persistent record of one agent execution. Carries: agent name + version, prompt hash, provider, input, output, cost, latency, token usage, status, error info (if any), tenant id, timestamps, optional workflow_run_id + node_id for workflow runs.

Same shape across SQLite + Postgres. Every endpoint that reports on runs (`mdk logs`, `GET /runs/{id}`, `mdk trace replay`) reads RunRecords.

### What's the difference between `mdk serve` and `mdk worker`?

- **`mdk serve`** — HTTP runtime. Exposes `POST /run`, `GET /jobs/{id}`,
  `GET /runs/{id}`, `GET /agents`, `/healthz`, `/ready`. Accepts jobs
  via authenticated HTTP, persists to storage as `QUEUED`. Doesn't
  execute jobs itself.
- **`mdk worker`** — Polls the storage for queued jobs (via
  `claim_next_job` with `FOR UPDATE SKIP LOCKED`), runs them through
  the executor, marks as `SUCCESS` / `ERROR` / `SAFETY_BLOCKED` /
  `DEAD_LETTER`. Notifies on terminal status if `notify_email` set.

Both processes need access to the same storage. Both scale
independently — typically 2-3 API replicas + N workers driven by queue
depth (KEDA Postgres scaler).

### What's the difference between an "agent" and a "workflow"?

- **Agent**: one prompt → one model call (or one tool-use loop) → one
  validated output.
- **Workflow**: linear sequence of agents passing state through typed
  edges. Each node is an agent; each edge is a state transformation.

Workflows are declared in `workflow.yaml` and live alongside agents in
the same project. v0.3 ships sequential workflows only; conditional
routing + parallel + HITL land in v1.1 via LangGraph (the workflow IR
is forward-compatible).

### What's a "skill" in MDK?

A reusable callable an agent can invoke during a turn — a tool.
Three backend kinds:

- **`python`** — a Python function in your repo
- **`http`** — a REST API endpoint (any URL the runtime can reach)
- **`mcp`** — a Model Context Protocol server (stdio-spawned subprocess)

Each skill is its own folder under `skills/<name>/` with a `skill.yaml`
declaring its schema + cost + side-effects category. Agents reference
skills by name in `agent.yaml: skills:`.

### How does MDK handle failure modes?

A typed failure taxonomy:

| FailureType | Retry? | Fallback? | Example |
|---|---|---|---|
| `RateLimitError` | yes (with backoff + retry_after) | yes (next in chain) | "429 from Anthropic" |
| `ModelUnavailableError` | yes | yes | "Bedrock is down" |
| `MovateTimeoutError` | yes (limited) | yes | "Model took >30s" |
| `ContextLengthError` | no | yes | "Prompt > 200k tokens" |
| `ContentFilterError` | no | no | "Provider's safety filter triggered" |
| `SchemaError` | no | no | "Agent output didn't match output.json" |
| `AuthError` | no | no | "API key revoked" |
| `BudgetExceededError` | no | no | "Run cost > max_cost_usd_per_run" |
| `PolicyViolationError` | no | no | "Model not in allowed_providers" |
| `TenantBudgetExceededError` | no | no | "Tenant's monthly cap hit" |

Operator can override via the retry policy file; defaults are sensible.

### What's the `--mock` flag?

A hermetic provider (`MockProvider`) that returns a fixed JSON without
calling any real model. Used for:
- CI tests (no API keys, no costs, deterministic)
- Local dev (instant feedback)
- Eval gates (`mdk eval --mock`) for the "did the schema validation
  pass?" check independent of model behavior

`MOVATE_MOCK_RESPONSE` env var overrides the mock's reply.

### What's the eval gate and how does it work?

```bash
mdk eval ./faq-agent --gate 0.7
# → exit 0 if mean accuracy >= 0.7, exit 1 otherwise
```

Per-case scoring with two judges:
- **Exact match** — JSON equality vs. `expected` field in the dataset
- **LLM-as-judge** — cross-family judge model scores against a rubric

Multiple runs per case (`--runs 3+`) defeats LLM sampling variance.

v0.6+ also scores **four dimensions** per case: accuracy /
faithfulness / coverage / latency. Dataset opt-in via `grounding`,
`expected_coverage`, `latency_budget_ms` fields. Gate stays on accuracy
alone for back-compat; future flags add per-dim gating.

### What's a "context" in MDK?

A reusable prompt fragment under `contexts/<name>.md`. An agent
references it via `agent.yaml: contexts: [<name>]`; the body gets
prepended to the agent's prompt at render time.

Use case: a customer has 30 agents that all need the same brand-voice
guidelines. Put it in `contexts/brand-voice.md`, reference from each
agent. One edit updates all 30.

### What's a "tenant" in MDK?

A logical owner of resources. Every run, job, eval, api-key,
workflow-run carries a `tenant_id`. Every storage method that touches
per-tenant rows filters by `tenant_id` at the SQL layer (audited; see
v1.0 stage 4 in BACKLOG.md).

Local CLI runs stamp `tenant_id="local"`. Server-side runs carry the
authenticated caller's tenant. Multi-tenant deployments configure
`policy.yaml: tenants:` to declare known tenants + per-tenant budget caps.

### How does authentication work?

API keys in the format `mvt_<env>_<tenant_prefix>_<keyid>_<secret>`.
Example: `mvt_live_acme1234_KEYID12345_secretXXXXXXXXX`.

- `env`: `live` or `test` — the runtime checks against its own env
- `tenant_prefix`: first 8 hex chars of the tenant uuid
- `keyid`: 12 random base32 chars (the revocation handle)
- `secret`: 40-50 url-safe-base64 chars (bcrypt-hashed at rest)

`mdk auth create-key --tenant <uuid>` mints; `mdk auth revoke-key <keyid>`
revokes. JWT support for Teams bot inbound webhook is a separate
hardening PR (signed by Microsoft, validated against their JWKS).

### What's the difference between policy.yaml, runtime.yaml, eval.yaml, knowledge.yaml?

Project-wide config split (the **canonical config split**, v0.6+):

| File | Owns |
|---|---|
| `policy.yaml` | `policy:` (allowed_providers, deny_models, max_cost), `defaults:`, `agents_dir:`, `workflows_dir:` |
| `runtime.yaml` | `runtime:` (which runtimes are allowed: litellm, native_anthropic, etc.) |
| `eval.yaml` | `eval:` + `bench:` (gate thresholds, judge defaults, model comparison list) |
| `knowledge.yaml` | `knowledge:` — stub for v0.7+ RAG config |

All four are optional. Dedicated-file-wins; one-shot deprecation
warning if both `policy.yaml` and a dedicated file carry the same
block. Operators migrate incrementally.

### How do I run an agent against a deployed Movate runtime?

```bash
mdk config add-target prod \
  --url https://movate-prod-api.azurecontainerapps.io \
  --key-env MOVATE_PROD_KEY \
  --set-active

mdk submit faq-agent '{"question": "what is movate?"}' --wait
# → Queues job, polls until terminal, prints the result.
```

Without `--wait`: fire-and-forget. Without `--target`: uses the active
target from `~/.movate/config.yaml`.

---

## §3 — Advanced (architecture + extension)

### How is the codebase organized?

```
src/movate/
├── cli/                  # Typer commands (mdk init / validate / run / eval / serve / ...)
├── core/                 # The agent execution engine — provider-agnostic
│   ├── executor.py       # The big orchestrator
│   ├── models.py         # Pydantic AgentSpec / RunRecord / JobRecord / etc.
│   ├── loader.py         # YAML → AgentBundle (with schemas + prompt template)
│   ├── workflow/         # Workflow IR + compiler + runner
│   ├── eval.py           # 4-dim scoring + LLM-as-judge + dataset loader
│   ├── retry.py          # Retry policy (backoff + retry_after honoring)
│   ├── skill_*.py        # Tool-use infrastructure
│   └── auth.py           # API key parsing + verification
├── providers/            # LLM adapters behind BaseLLMProvider
│   ├── litellm.py        # Default — covers ~40 providers
│   ├── anthropic.py      # Native Anthropic SDK
│   ├── openai_native.py  # Native OpenAI SDK
│   ├── langchain_native.py
│   ├── mock.py           # Hermetic test provider
│   └── pricing.yaml      # Canonical cost table
├── storage/              # Storage backends behind StorageProvider
│   ├── sqlite.py         # Local dev
│   ├── postgres.py       # Production
│   └── base.py           # Protocol
├── runtime/              # FastAPI server + worker (HTTP-mode)
├── teams_bot/            # Microsoft Teams integration (v0.7)
├── tracing/              # OTel + Langfuse + stdout
└── templates/            # `mdk init` agent scaffolds
```

Three Protocols cleanly separate concerns: `BaseLLMProvider`,
`StorageProvider`, `Tracer`. Every concrete impl passes the same
conformance suite.

### What's the executor's pipeline?

1. **Load** agent.yaml → AgentBundle (validated)
2. **Render** prompt with input via Jinja2
3. **Validate** input against the agent's input schema (Draft 2020-12)
4. **Check** budget + policy + tenant budget
5. **Pick** provider from registry (based on agent's `runtime` field)
6. **Call** provider.complete() with retry + fallback chain
7. **(Tool-use loop)** — if the model emits tool_use, dispatch the skill,
   feed the result back, loop until final or max-turns guard
8. **Parse** model output (JSON)
9. **Validate** against output schema
10. **Save** RunRecord + emit spans (Langfuse + OTel)
11. **Return** RunResponse

If any step fails, save a FailureRecord; surface the typed error to
the caller.

### What's the tool-use loop architecture?

Per ADR 002 D1: **the loop lives in the Executor**, not in the provider.
Providers are stateless `complete()`s that return one of two shapes:

- `CompletionResponse(kind="final", text=...)` → loop exits, that's the answer
- `CompletionResponse(kind="tool_use", tool_name=..., tool_id=..., tool_input=...)` → loop dispatches the skill, appends the result, loops

Skill backends dispatch by `implementation.kind`:
- `python` → `importlib` resolve + `await func(input, ctx)`
- `http` → `httpx.post(entry, json=input)`
- `mcp` → JSON-RPC over stdio to the spawned subprocess

Tool specs flow `agent.yaml: skills: → SkillBundle → provider.to_tool_spec → request.tools`.
Each native provider translates the executor's OpenAI-shaped message
history into its own wire format (Anthropic content blocks; OpenAI
flat-message). The executor is format-agnostic.

### What's the workflow IR designed for?

A directed acyclic graph of nodes (agents) + edges (state transformations).
v0.3 ships only linear chains; the IR has fields for conditional routing
+ parallel + HITL that v1.1's LangGraph compiler will emit against.

The cardinal rule (from ADR 002 D1 generalized): **workflow logic lives
in the runner**, providers stay stateless. The IR is structural; the
runner walks it.

### How does eval reconcile LLM-as-judge variance?

Three knobs:

- **`--runs N`** — repeat each case N times; score reflects the
  distribution
- **`--gate-mode`** — `mean | min | p10`. p10 is the practical
  default for production (penalizes outliers without hard-min
  sensitivity)
- **Cross-family judge enforcement** — `assert_cross_family` in
  `core/eval.py` refuses to let an openai-family agent be judged by
  another openai-family model. Defeats the self-preference bias the
  literature has documented.

For deterministic schemas (classifiers, extractors), use `judge: exact`
instead of LLM-as-judge; faster + free + zero variance.

### What's the cost-drift detection?

`LiteLLMProvider` records both the cost from LiteLLM's response AND
the cost MDK computes from its own pricing table. If they diverge by
>5%, we log loud — usually means LiteLLM updated their pricing knowledge
faster than our `pricing.yaml`. The pricing.yaml is the **canonical
source for billing**; LiteLLM's cost is just a sanity check.

Update flow: when a provider raises prices, the pricing.yaml PR carries
a `last_verified` date so the next operator knows when it was checked.

### How does the workflow runner persist state?

Per-node `RunRecord`s linked by `workflow_run_id`. The runner walks
topological order; if a node fails, the workflow stops at that node and
the operator sees partial state via `mdk show <workflow-run-id>`.

A failed node can be re-run alone via `mdk run --replay <node-run-id>`;
the workflow runner has a `--resume <workflow-run-id>` option that
picks up where the last successful node left off (v0.3+).

### What's the canonical config split's resolution order?

1. Read `policy.yaml` (legacy single-file)
2. For each dedicated file present (`runtime.yaml`, `eval.yaml`,
   `knowledge.yaml`), overlay the relevant block — **dedicated wins**
3. If both define the same block, emit a one-shot deprecation warning
   pointing at the dedicated file
4. Merged result feeds the Executor + every other consumer via
   `load_project_config()`

The operator can stay on the unified `policy.yaml` indefinitely; the
split is opt-in.

### How do I plug in a new provider?

1. Implement `BaseLLMProvider` Protocol in `src/movate/providers/<name>.py`
2. Add the runtime kind to `AgentRuntime` enum (`src/movate/core/models.py`)
3. Register the adapter in `cli/_runtime.py` opportunistically (optional dep)
4. Add a pricing entry in `providers/pricing.yaml`
5. Tests against your `_FakeClient` (see `tests/test_anthropic_provider.py`
   for the pattern)
6. Document in `docs/stack-defense.md` + `pyproject.toml [<name>]` extra

The Protocol is small: `complete`, `stream`, `embed`, `pricing_key`,
`to_tool_spec`. ~150 lines total per adapter typically.

### How do I plug in a new skill backend?

Same pattern as providers — implement `SkillBackend` Protocol in
`src/movate/core/skill_backend/<kind>.py`, register at module import,
add tests.

Existing kinds: `python`, `http`, `mcp`. A `langchain_tool` kind for
wrapping LangChain BaseTool instances is on the backlog.

### How does MDK's storage layer support both SQLite and Postgres?

`StorageProvider` Protocol in `src/movate/storage/base.py`. Every
concrete impl (`SqliteProvider`, `PostgresProvider`, `InMemoryStorage`)
passes the same conformance suite (parametrized in `tests/test_storage_conformance.py`).

Tenant isolation: every method that reads / writes per-tenant rows
requires + filters by `tenant_id` at the SQL layer (audited in v1.0
stage 4). Cross-tenant reads return `None` (not 403 — don't leak id
existence).

### How does `mdk deploy` work?

Wraps `az acr build` (cloud-side image build) + `az containerapp update`
for both the API + worker, then polls `GET /healthz` until the version
field matches the just-built image.

Image-tag default: `movate:<version>-<git-sha-short>`. Rollback:
`mdk deploy --skip-build --image-tag movate:<previous-sha>`.

GH Actions deploys via federated OIDC (no stored client secrets);
triggered on push to `release/<env>` branch. Per-env GitHub Environments
gate approvals.

---

## §4 — Operations

### What does `mdk doctor` check?

A deep environment health check:

- Python version + required + optional deps
- API key presence (per provider)
- SQLite path writable
- `pricing.yaml` version + age
- `movate.yaml` discovery
- (With `--target <name>`) Azure deploy path: `az` install → login →
  subscription match → RG → ACR → API + worker `/healthz`

Output is operator-facing with copy-pasteable fixes per red row.

### What logs does MDK produce?

Three streams:

- **stdout** — only the actual command output (JSON / table / etc.) so it pipes cleanly
- **stderr** — operator-facing messages (progress, hints, errors) via `structlog`
- **Tracer** — JSON spans to a configurable backend (Langfuse, OTel)

CLI logs default to INFO level. `-v` flips to DEBUG; `-q` to WARNING.

### How does MDK handle secrets?

Three layers:

- **Local dev** — `.env` file picked up by `python-dotenv`. `.env.example` ships in the repo
- **Server-side** — env vars set by the orchestrator (ACA reads from KV; k8s reads from a Secret); never logged
- **Per-user Teams binding (3.1.c)** — Fernet-encrypted at rest in sqlite; encryption key from `MOVATE_TEAMS_ENCRYPTION_KEY` env

Plaintext API keys exist only briefly inside `dispatch_skill` / `MovateClient`. The codebase never logs them; failures log only the key id (last 4 chars) for diagnostics.

### How does observability work?

Three opt-in tracers:

- **`StdoutTracer`** — default; JSON spans to stderr. Useful for local dev
- **`LangfuseTracer`** — when `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` are set. Full prompt/response/cost/latency dashboard
- **`OtelTracer`** — when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Distributed traces to any OTLP backend

Both can be on simultaneously (different consumers want different views).

### What happens when a job exceeds its budget?

`max_cost_usd_per_run` on the agent + `monthly_usd` on the tenant budget. Both checked at executor entry; budget-exceeding runs short-circuit BEFORE any provider call (zero cost incurred on a rejected run).

Failure type:
- `BudgetExceededError` — agent's per-run cap
- `TenantBudgetExceededError` — tenant's monthly cap

Neither retries; neither falls back. The cap is the cap.

### How does retry / fallback work?

Per-error-type policy in `core/retry.py`:

- Retryable errors (rate-limit, model-down, timeout) retry with exponential backoff + jitter, capped at 5 attempts
- Rate-limit retries honor the `Retry-After` header
- After retries exhaust, fall through to the next provider in `model.fallback` chain
- If the whole chain exhausts: terminal `ERROR` with the last failure's reason

`auth_error`, `safety_blocked`, `schema_error`, `context_length`, `budget` never retry — they're deterministic and a retry won't help.

### How does the worker claim jobs?

`SELECT ... FOR UPDATE SKIP LOCKED` on the `jobs` table (Postgres) or row-level `BEGIN IMMEDIATE` (SQLite). Multiple workers can run concurrently without claiming the same job.

Retry-aware: `claim_next_job` skips rows whose `next_retry_at > now()` (partial index makes this fast).

### What's KEDA Postgres scaling?

Container Apps worker scales based on **queue depth**, not CPU. KEDA polls the `jobs` table; when `count(*) WHERE status='queued' AND next_retry_at < now()` exceeds `queueDepthPerReplica`, a new replica spins up.

Why: CPU-utilization scaling is a lagging indicator (load visible AFTER CPU rises); queue depth leads (load visible BEFORE any pod's CPU rises). For bursty Teams demos, that matters.

### How does `mdk eval --baseline` work?

Diffs the current eval result against a stored baseline (either sqlite-stored by id or `--baseline-file <path>` for CI-friendly JSON). Computes mean_score delta + pass_rate delta + cost delta + sample_count delta.

Regression detection: if mean_score or pass_rate dropped past `--regression-tolerance` (default 0.0), exit 1.

The CI flow: on merge to main, write a fresh baseline; on every PR, diff against the committed baseline.

---

## §5 — Eval + quality

### How is `accuracy` scored?

For `judge: exact`: deep dict equality between agent's `output` and the case's `expected`. Numbers compared with `pytest.approx`-style tolerance; strings compared verbatim (no whitespace normalization — that's the user's job in the dataset).

For `judge: llm_judge`: an LLM (cross-family from the agent's model) scores 0-1 against a rubric. Cross-family enforcement avoids self-preference bias (OpenAI agent + OpenAI judge = inflated scores per Zheng et al. 2023).

### How is `faithfulness` scored?

LLM judge against optional `grounding` field on the dataset row. The judge prompt asks: "Does the actual output stay true to the grounding context?" Scores 0 (contradicts) to 1 (every claim supported).

Skip when `grounding` is absent. Cases that don't opt in don't get a faithfulness score.

### How is `coverage` scored?

Deterministic substring match against `expected_coverage` (list of topics). Score = fraction of topics present in the JSON-stringified output (case-insensitive). Cheap and reliable for "did the answer mention X, Y, Z?"

### How is `latency` scored?

Linear: 1.0 within budget, decays to 0.0 at 2× budget. Budget = case's `latency_budget_ms` if set, else the agent's `timeouts.call_ms`. Always scored on successful runs.

### How does the eval gate handle multiple dimensions?

The gate today is **accuracy-only** (v0.6 back-compat). Other dims are reporting-only — they surface in the breakdown table + JSON but don't fail the build.

Roadmap: `--gate-faithfulness 0.8` / `--gate-coverage 0.7` flags for per-dim gating. Multi-dim aggregate via `DimensionScores.aggregate()` is already in the model; just needs the CLI flag.

### What's a "objective" in eval?

A named subset of dataset cases with its own threshold + judge:

```yaml
objectives:
  - id: routing-accuracy
    description: Tickets land in the right category
    threshold: 0.85
    judge: exact
  - id: tone-friendly
    description: Replies sound helpful
    threshold: 0.7
    judge: llm_judge
```

Dataset rows tag themselves with `objective: routing-accuracy`. `mdk eval --objective routing-accuracy` runs only those cases with that objective's gate. CI fails PRs that regress one objective even if overall pass-rate looks fine.

### How does `mdk bench` differ from `mdk eval`?

- **`mdk eval`** — single agent across its full dataset; produces a pass/fail verdict
- **`mdk bench`** — single agent (or single input) across **multiple providers**; produces a comparison table with cost / latency / score per provider

`bench` is for "which model should we use?"; `eval` is for "does this agent meet our quality bar?".

---

## §6 — Security + multi-tenancy

### How does tenant isolation work?

Every storage method that reads or writes per-tenant rows REQUIRES a
`tenant_id` argument and filters at the SQL `WHERE` clause level.
`get_run(run_id, tenant_id=...)` returns `None` for a run belonging to
another tenant — not 403, because that would leak the run id's existence.

v1.0 stage 4 audited every storage method against the 3 backends
(SQLite, Postgres, in-memory). Tests in `test_tenant_isolation.py` mint
two tenants, write parallel rows, then sweep every cross-tenant read
path asserting tenant B can never see tenant A's data.

### How does authentication work in HTTP mode?

Bearer token in `Authorization` header. Format:
`Bearer mvt_<env>_<tenant>_<keyid>_<secret>`.

The auth middleware:

1. Parses the key (rejects malformed → 401)
2. Looks up the key record by `key_id`
3. Verifies the secret via bcrypt
4. Checks the key isn't revoked
5. Cross-checks the parsed `tenant_prefix` against the record's `tenant_id`
6. Stamps the resolved `tenant_id` on the request for downstream handlers

Audit trail: every job records `api_key_id` so the audit log shows
which key issued each run.

### What's the policy enforcement layer?

`policy.yaml: policy:` with three knobs:

- `allowed_providers: [openai/, anthropic/]` — denylist anything else
- `deny_models: [openai/gpt-3.5-*]` — pin minimum quality bar
- `max_cost_per_run_usd: 0.50` — runtime cost ceiling

Enforced at TWO layers:

- `mdk validate` — static gate on every agent.yaml. Operator sees
  violations in the per-agent PR
- `Executor.execute()` — runtime gate. Bundles loaded by `mdk serve`
  can't bypass; denied runs short-circuit before any provider call (zero
  cost incurred)

### How does rate limiting work?

Token-bucket per API key. `core/rate_limit.py` ships `InProcessRateLimiter`
(local memory) + `NoOpRateLimiter` (disabled). Redis backend slots in
behind the Protocol for multi-replica deployments.

Defaults: 60 req/min/key. Configurable via `mdk serve
--rate-limit-per-minute` or `MOVATE_RATE_LIMIT_PER_MINUTE` env. `0`
disables.

Response headers on every authenticated request: `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, `X-RateLimit-Reset`. On rate-limit: 429 +
`Retry-After`.

### How does the Teams bot handle per-user keys?

`/movate connect <api-key>` in a DM binds the Teams user's AAD object
id to a Movate API key. Key encrypted at rest via Fernet
(`MOVATE_TEAMS_ENCRYPTION_KEY` env).

When the user runs `@movate run` in a channel: the bot looks up their
binding, decrypts the key, builds a per-user `MovateClient` (LRU-cached),
and submits the job with THAT key. `RunRecord.created_by` reflects the
correct user.

Three modes:
- Default: unbound users fall back to the bot's fleet key
- Strict (`--require-binding`): unbound users get a "please connect" card
- Disabled (`--no-identity`): every user uses fleet key (smoke-test mode)

### Is MDK SOC2-compliant?

Building blocks are present: audit log (RunRecord + FailureRecord),
tenant isolation, encrypted secrets, role-scoped API keys, OTel traces.
Formal SOC2 audit hasn't been pursued — waiting for the first enterprise
customer engagement that requires it. Movate IT can scope when it lands.

---

## §7 — Roadmap + comparison

### What's shipped vs. planned?

See README's "Status" table and BACKLOG.md. Snapshot:

- **v0.1** — single agent, init/validate/run, SQLite
- **v0.2** — eval (exact + LLM judge), bench
- **v0.3** — sequential workflows
- **v0.4** — Langfuse + OTel + trace replay + regression detection
- **v0.5** — HTTP runtime + worker + Postgres + Azure deploy stage 1
- **v0.6** — Skills + Contexts (ADR 002) + canonical config split +
  4-dim eval + side_effects policy
- **v0.7 (current)** — Teams integration (ADR 003)
- **v0.8** — eval-with-upload, parallel tool-use, Helm chart, pgvector
- **v0.9** — Apache AGE + d3.js KG viz
- **v1.0** — Azure GA, JWT hardening, prod-tenant cutover
- **v1.1+** — LangGraph swap-in (conditional + parallel + HITL), tools/skills marketplace

### How does MDK compare to LangChain?

| | MDK | LangChain |
|---|---|---|
| **Primary artifact** | `agent.yaml` (declarative) | Python code |
| **Eval** | Built-in (4-dim, gating, baseline diff) | Add-on (LangSmith / community) |
| **Multi-provider** | LiteLLM + native SDKs | LangChain abstractions per provider |
| **Workflows** | Declarative IR; LangGraph compiler in v1.1 | LangGraph (native) |
| **Production runtime** | FastAPI + worker, Postgres-backed | DIY |
| **Deploy** | `mdk deploy` → ACA via Bicep | DIY |
| **License** | Proprietary (Movate) | MIT |

We're not anti-LangChain — `runtime: langchain` wraps a LangChain
Runnable as an MDK agent. The integration story is "use MDK as the
production scaffold; bring your LangChain Runnable if you have one."

### How does MDK compare to LlamaIndex?

LlamaIndex is RAG-focused; MDK is agent-runtime-focused. Different
scope. When MDK's pgvector + AGE land in v0.8/v0.9, MDK will cover
RAG natively; if a customer has existing LlamaIndex pipelines, the
`http` skill kind can wrap a LlamaIndex service.

### How does MDK compare to Vellum / Helicone / Lakera?

These are hosted services. MDK is the in-VPC OSS+Movate-IP stack.
Different ICP — MDK wins for customers who can't send data to a hosted
third party (financial services, healthcare, government, EU GDPR
sensitive). Vellum/Helicone wins for teams that don't have ops capacity
to host anything themselves.

### What's the licensing story for an MDK-built deliverable?

MDK itself is Movate proprietary. The dependencies MDK uses are all
permissively licensed (MIT, Apache 2.0, BSD, PostgreSQL License). A
customer engagement that ships an MDK-built agent system:

- Includes Movate's framework code (licensed via the engagement contract)
- Includes the OSS dependencies (permissive, customer can embed + commercialize)
- Does NOT include any GPL / AGPL / SSPL / BSL contamination

See [`docs/license-posture.md`](license-posture.md) and
[`docs/stack-defense.md`](stack-defense.md) for the full defense.

### Can a customer self-host MDK without Movate?

Today: no — MDK is delivered as part of a Movate engagement. The
framework code is proprietary; private artifact distribution only.

Future (post-v1.0): Helm chart for self-hosted k8s (Tier 9 backlog),
SaaS tenant onboarding flow. Both gated on customer pull; no
commitment to public OSS release of MDK itself.

### What's NOT in MDK?

Deliberate non-goals:

- **A model.** MDK is provider-agnostic; bring your own.
- **A UI for end users.** Teams bot is one interface; everything else
  is CLI or API. A web SPA could land (post-v1.0) but isn't a current
  priority.
- **A prompt marketplace.** Templates live in your repo; no shared
  catalog.
- **An autonomous-agent framework.** MDK runs single-turn or
  tool-using or linear-workflow agents. Long-running autonomous loops
  (BabyAGI-style) are explicitly out of scope.
- **Fine-tuning orchestration.** Off-the-shelf models only; fine-tunes
  via your model provider's tools.

---

## How to extend this FAQ

When you answer the same question twice in Slack / a meeting / a customer call: add it here. Open a PR. Each entry should be:

- A real question someone asked (not a hypothetical)
- Answered in 2-3 sentences in the right section
- With a link to deeper docs if the topic has them (ADRs, README sections, source files)

Each new entry pays for itself the second time someone asks. Compound interest on documentation.
