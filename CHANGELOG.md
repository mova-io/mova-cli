# Changelog

All notable changes to movate. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows [SemVer](https://semver.org/).

## [0.5.0] — 2026-05-09

**movate is now a service.** v0.5 takes the framework from "library +
local CLI" through queue → auth → HTTP → worker → Postgres, in five
incremental stages:

| stage | what shipped |
|---|---|
| 1 | Job queue data layer (`JobRecord` + `jobs` table + claim semantics) |
| 2 | API key auth crypto + storage + `movate auth create-key | list-keys | revoke-key` |
| 3a | FastAPI runtime with `/healthz`, `POST /run`, `GET /jobs/{id}` + auth middleware |
| 3b | Agent registry + `GET /agents` + `movate serve` (uvicorn binding) |
| 4 | Worker claim loop + `movate worker` — climactic deliverable; movate stops being a queue and becomes a runtime |
| 5 | PostgresProvider port — production-ready storage with `SELECT ... FOR UPDATE SKIP LOCKED` for true worker parallelism |

**121 new tests across the release** (412 unit + 3 smoke when PG is
configured; 391/3 without). End-to-end binary smoke validated against
both backends: `movate serve` + `movate worker` in two real processes,
job lifecycles QUEUED → RUNNING → SUCCESS in ~12-100ms.

### Added — PostgresProvider (v0.5 stage 5)

**v0.5 capabilities are now feature-complete on both backends.**

- **`storage/postgres.py`** — full Protocol parity with
  `SqliteProvider`, against `asyncpg`. Schema uses `JSONB` (queryable,
  indexable) instead of TEXT-with-JSON, `TIMESTAMPTZ` instead of ISO
  strings, `BOOLEAN` instead of `INTEGER`. Per-connection pool init
  registers a `json.dumps`/`json.loads` codec for `jsonb` so handlers
  pass and receive plain dicts.
- **`claim_next_job` uses `SELECT ... FOR UPDATE SKIP LOCKED`** —
  superior to sqlite's `BEGIN IMMEDIATE`. Multiple workers truly
  run in parallel: each takes a row-level lock on a different row,
  no global serialization. New test
  `test_postgres_claim_skip_locked_runs_concurrent` proves both
  workers grab two distinct rows concurrently (sqlite would block
  one of them).
- **`build_storage()` switches on `MOVATE_DB_URL`** —
  `postgres://` / `postgresql://` URLs route to `PostgresProvider`;
  otherwise falls back to `SqliteProvider`. `asyncpg` is imported
  lazily so sqlite-only deployments don't need it installed.
- **`tests/conftest.py`** now provides a shared `storage` fixture
  parametrized over `(memory, sqlite, postgres)`. PG params skip
  automatically when `MOVATE_PG_TEST_URL` is unset, so devs without
  a local PG see clean test runs and CI can wire a service-container
  job to exercise that branch. Per-test truncation of the PG state
  keeps tests hermetic without re-creating the schema.
- 21 new test invocations: 16 conformance tests now run against
  Postgres in addition to sqlite + memory; one new PG-specific
  concurrent-claim test for SKIP LOCKED.

### Fixed — Two real bugs surfaced by the PG smoke walk-through

- **asyncpg pool was created on the wrong event loop.**
  `cli/serve.py` was doing `asyncio.run(storage.init())` (creates
  pool on a temporary loop, which then exits), then `uvicorn.run(app, ...)`
  (creates a different loop). asyncpg connections are bound to
  their creation loop; this manifested as "another operation is in
  progress" 500s on the first request. Restructured to do
  `asyncio.run(_run_serve(...))` where `_run_serve` is async and
  uses `uvicorn.Server.serve()` so init + serve share one loop.
- **Fire-and-forget `touch_api_key` raced asyncpg pool RESET.**
  The auth middleware was scheduling `asyncio.create_task(_safe_touch(...))`
  after a successful auth. Under asyncpg pool semantics, this could
  re-acquire the same connection that was mid-RESET (called by pool
  release after the previous `get_api_key`), triggering the same
  "another operation is in progress" error. Moved to inline
  `await _safe_touch(...)` — the latency cost is sub-millisecond
  vs the cost of a flaky service. Also made the corresponding test
  deterministic (no skip-on-race).

### Added — Worker claim loop + `movate worker` (v0.5 stage 4)

**movate is now a runtime, not just a queue.** The full HTTP →
queue → claim → execute → terminal-state lifecycle works end-to-end
between two real processes.

- **`runtime/dispatch.py`** — `WorkerDispatch.execute_job(job) →
  DispatchOutcome`. Pure logic, no async loop. Agent + workflow
  paths; unknown target / executor crash both → terminal ERROR with
  structured error info. The split keeps tests deterministic
  (assert each branch with one call) and makes the loop trivial.
- **`runtime/worker.py`** — `Worker.run_one_cycle()` (deterministic;
  one claim+dispatch+update; tests call this directly) and
  `Worker.run_forever(stop_event)` (CLI loop, sleeps the configured
  poll interval when the queue is empty, exits promptly on event
  set even mid-poll). Never crashes on a single bad job: dispatch
  errors and storage update failures both get logged and the loop
  continues.
- **`runtime/registry.scan_workflows(path)`** — mirrors
  `scan_agents`. Returns name → `WorkflowGraph`; one broken
  workflow.yaml warns and skips rather than crashing startup.
- **`movate worker`** CLI replaces the stub. Flags: `--tenant-id`
  (drain a single tenant; default is all), `--agents-path` (env:
  `MOVATE_AGENTS_PATH`), `--workflows-path` (env:
  `MOVATE_WORKFLOWS_PATH`), `--poll-interval`, `--mock`. Registers
  SIGINT/SIGTERM handlers that flip the stop event so in-flight
  jobs finish before exit.
- **`RunResponse` gained `run_id`** — populated by `Executor.execute`
  for both success and error paths. The worker reads it to mirror
  into `JobRecord.result_run_id`. Backwards compatible: empty
  string default.
- **`runtime/worker.WorkerConfig`** — `poll_interval_seconds` and
  optional `tenant_id`. Workers without a `tenant_id` drain all
  queues (operator/dev mode); tenant-bound workers are the
  production pattern.
- **End-to-end binary smoke** validated: scaffold an agent, start
  `movate serve --port 8766` and `movate worker --mock` in
  separate processes, mint a key, POST /run → 202 queued, poll
  /jobs/{id} → `status: success`, `result_run_id` matches the
  persisted `RunRecord.run_id`, total lifecycle ~112ms (claim
  ~106ms after submission, completed ~6ms after claim).
- 10 new tests across dispatch (agent success/error/unknown,
  workflow with real one-node yaml on disk, executor crash →
  internal error) and worker (claim/empty, drain one job, unknown
  target → ERROR, tenant scoping, run_forever exits on stop event
  even with long poll interval).

### Added — Agent registry + `movate serve` (v0.5 stage 3b)

- **`runtime/registry.py`** — `scan_agents(root)` walks one level
  deep for directories containing `agent.yaml`, loads each via the
  existing `load_agent`, sorts by spec name. Invalid agents (broken
  YAML, unknown api_version, etc.) are skipped with a warning log
  rather than crashing — one bad agent shouldn't blackhole the
  catalog at runtime startup.
- **`GET /agents`** endpoint returns name/version/description
  metadata only. Auth-required for consistency. Per-tenant agent
  visibility is post-v0.5 — every authenticated tenant currently
  sees the same catalog (sufficient for a single-team deployment).
- **`movate serve`** replaces the v0.5 stub with a real uvicorn
  binding. Flags: `--host` (default `127.0.0.1`), `--port` (default
  `8000`), `--agents-path` (env: `MOVATE_AGENTS_PATH`, default
  `./agents`), `--log-level`. Storage is pre-init'd on the parent
  loop so aiosqlite connections aren't bound to a dead loop;
  registry is scanned once at startup so each `/agents` request is
  a constant-time list lookup.
- 11 new tests: 8 registry edge cases (missing/file/empty roots,
  one-level walk, sibling-skip, partial-failure tolerance),
  3 `/agents` endpoint cases (empty registry, metadata-only
  response, auth required).
- **End-to-end binary smoke** validated against the real `movate`
  binary: `serve` boots → `/healthz` returns 200 → `auth
  create-key` mints a key → `/agents` lists scaffolded agents →
  `POST /run` returns 202 with job_id → `GET /jobs/{id}` returns
  the queued state. The full HTTP→storage→auth chain works.

### Added — FastAPI runtime (v0.5 stage 3a)

- **`runtime/`** package — thin HTTP layer over the storage Protocol
  and `core/auth`. Wire schemas (`runtime/schemas.py`) live separately
  from `core/models.py` so API and DB can evolve independently.
- **`build_app(storage)`** factory — `runtime/app.py` returns a
  FastAPI app bound to a given storage backend. Tests pass an
  `InMemoryStorage`; `movate serve` (lands stage 3b) will pass a
  `SqliteProvider`. The factory pattern means there's no global
  app object and no env-var gymnastics.
- **Endpoints:** `GET /healthz` (unauthed liveness), `POST /run`
  (queue a job → 202 with `job_id`), `GET /jobs/{id}` (poll; returns
  the current JobRecord state minus `api_key_id`).
- **Auth middleware** (`runtime/middleware.py`) composes the stage-2
  primitives: `parse_api_key` → `storage.get_api_key` →
  `check_record`. Every failure mode collapses to a uniform `401`
  with `{"error": {"code": "auth_required", "message": "..."}}` —
  the discriminator is logged but never echoed (timing-oracle
  defense). Successful auth fires-and-forgets `touch_api_key` so
  `last_used_at` reflects calls without blocking responses.
- **`AuthContext`** dataclass — what handlers receive after a
  successful auth. Carries `tenant_id`, `api_key_id`, `env`. Handlers
  MUST NOT reach back to the underlying `ApiKeyRecord` (no plaintext
  secret on the wire ever).
- **Tenant scoping:** `GET /jobs/{id}` returns 404 (not 403) for
  cross-tenant lookups. 403 would let an attacker probe whether a
  `job_id` exists in another tenant.
- **`runtime/errors.py`** — single error envelope shape; codes are
  stable enums (`AUTH_REQUIRED`, `NOT_FOUND`, `BAD_REQUEST`,
  `INTERNAL`); messages may change between releases but codes are
  contract.
- 14 tests via `fastapi.TestClient` + `InMemoryStorage`: every auth
  failure mode → 401, /run persists tenant + key attribution onto
  the JobRecord, /jobs/{id} cross-tenant safety, request validation
  (422 on missing fields / unknown JobKind / empty target).

### Added — API key auth (v0.5 stage 2)

- **`core/auth.py`** — pure crypto, no I/O. `mint_api_key` produces a
  `mvt_<env>_<tenant_prefix>_<key_id>_<secret>` string with 256 bits
  of entropy in the secret. `parse_api_key` validates shape via regex
  (rejects malformed / wrong env / wrong tenant prefix length).
  `hash_secret` uses SHA-256 of `salt || secret`; `verify_secret` is
  constant-time via `hmac.compare_digest`. `check_record` is the
  decision tree for verification — returns `None` on success or a
  `VerificationFailure(reason=...)` for not_found / revoked /
  tenant_mismatch / env_mismatch / bad_secret. Each branch is unit
  tested in isolation.
- **`ApiKeyEnv` enum** — `live` | `test`, hard separation enforced at
  parse time before any DB hit. **`ApiKeyRecord`** Pydantic model
  carries `secret_hash`, `salt`, `created_at`, `last_used_at`,
  `revoked_at`, optional `label`. The plaintext secret is never
  stored.
- **`api_keys` table** added via SQLite migrations (idempotent). One
  partial index: `WHERE revoked_at IS NULL` — keeps `list_api_keys`
  fast as the table grows with revocations. Storage methods
  (`save_api_key`, `get_api_key`, `list_api_keys`, `revoke_api_key`
  idempotent, `touch_api_key` for last-used bump) on the Protocol +
  both backends.
- **`movate auth create-key | list-keys | revoke-key`** CLI surface.
  `create-key` prints the full key once on stdout (pipe into a
  vault) with a "save this now" warning on stderr. `--quiet`
  inverts the output streams for shell capture (`KEY=$(... --quiet)`).
  `list-keys` defaults to active keys; `--include-revoked` shows the
  full audit history. End-to-end smoked against the real binary
  with `MOVATE_DB=/tmp/...`: mint → list → revoke → list.
- 37 tests across pure crypto / storage round-trip / CLI integration.

### Added — Job queue data layer (v0.5 stage 1)

- **`JobRecord` + `JobKind`** in `core/models.py` — queue entry with
  agent/workflow discriminator, lifecycle status, optional
  `result_run_id` mirror back to the produced run, and `api_key_id`
  for audit. Re-uses the existing `JobStatus` enum so queue and run
  share one status vocabulary.
- **`jobs` table** added to the SQLite schema via `_MIGRATIONS` (so
  upgraders pick it up cleanly). Two indexes: `idx_jobs_queue_head`
  (partial, `WHERE status = 'queued'`) keeps `claim_next_job` O(queued);
  `idx_jobs_tenant_created` covers the `/jobs` listing path.
- **`save_job` / `get_job` / `list_jobs` / `claim_next_job` /
  `update_job`** on `StorageProvider` Protocol; implemented in
  `SqliteProvider` and `InMemoryStorage`.
- **Claim semantics:** FIFO oldest-first, status-guard (only
  `QUEUED` rows ever claimed), tenant-scoped, atomic via sqlite
  `BEGIN IMMEDIATE`. The Postgres provider (stage 5) uses
  `SELECT ... FOR UPDATE SKIP LOCKED` instead. 25 conformance tests
  (parametrized over both backends) cover CRUD round-trip, FIFO,
  status guard, tenant isolation, terminal-only `update_job`, and
  concurrent-claim no-double-dispatch on sqlite with two
  connections.
- **`update_job`** rejects non-terminal status transitions —
  `QUEUED`/`RUNNING` are owned by `save_job`/`claim_next_job`, so
  passing them is a programming error.
- Design decisions locked in
  [docs/v0.5-design.md](docs/v0.5-design.md) (queue claim model,
  multi-tenant isolation, API key format, workflow-in-queue
  dispatch, job→run linkage).

### Added — File-based eval baselines (CI integration)

- **`movate eval --baseline-file <path>`** — load an `EvalRecord` from a
  JSON file instead of looking up an `eval_id` in sqlite. Unblocks CI
  use: GitHub Actions runners are ephemeral, sqlite isn't. With this
  flag, the baseline can be a git-tracked artifact in the consumer's
  repo. Mutually exclusive with `--baseline`.
- **`movate eval --output-baseline <path>`** — after running, write the
  current run's `EvalRecord` to disk as JSON. Pair with the
  `refresh-baseline` job in CI on main-branch merge to keep the
  committed baseline current. Creates parent directories so users can
  drop the file at `.movate/<agent>/baseline.json` without pre-creating
  the dir.
- Example workflow at
  [.github/workflows/eval-gate.example.yml](.github/workflows/eval-gate.example.yml)
  with `gate-pr` (PR-time regression check) and `refresh-baseline`
  (main-branch refresh) jobs. Docs at
  [docs/ci-eval-gate.md](docs/ci-eval-gate.md). Six new tests cover
  load, write, mutual exclusion, missing/malformed JSON.

[0.5.0]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.5.0

## [0.4.0] — 2026-05-08

Observability + regression-detection. Closes both halves of the
"something changed; what?" loop: `eval --baseline` flags aggregate
score regressions; `run --replay` lets you re-execute the exact recorded
input against the current code. Plus a full tracing stack — Langfuse,
OTel, fan-out — and trace replay for post-mortem reconstruction.

**89 new tests this release** (288 unit + 3 smoke = 291 total). `ruff
format`, `ruff check`, `mypy src` (strict) all clean.

### Added — Tracing backends

- **LangfuseTracer** (`tracing/langfuse.py`) — wraps the Langfuse v2 SDK
  behind our `Tracer` Protocol. Optional dep (`pip install movate[langfuse]`).
  Fail-soft: tracer errors never break a run.
- **OtelTracer** (`tracing/otel.py`) — OTLP-HTTP exporter; span hierarchy
  workflow → node → provider call. Optional dep (`movate[otel]`). Lazy import.
- **CompositeTracer** (`tracing/composite.py`) — fan-out to N delegate
  tracers with per-delegate `SpanCtx` mapping; one bad backend can't kill
  siblings. Each delegate call wrapped in try/except.

### Added — Trace replay

- **`movate trace replay <id>`** (`cli/trace.py` + `core/replay.py`) —
  auto-detects agent vs workflow run by id, renders Rich tables (status,
  agent/workflow, latency, cost, tokens, error) plus per-node breakdown
  and initial/final state for workflows. `-v` shows full input/output JSON;
  `-o json` is pipe-friendly for diffs. New `get_run(run_id)` and
  `get_workflow_run(id)` lookups on `StorageProvider`.

### Added — Eval baseline diff

- **`movate eval --baseline <eval-id>`** (`core/baseline.py` + `cli/eval.py`)
  — closes the regression-detection loop. Diffs current eval vs a stored
  `EvalRecord` (mean_score, pass_rate, sample_count, cost). Renders a Rich
  diff table after the main eval output; `-o json` includes a `baseline`
  block. Exits 1 on regression past `--regression-tolerance` (default 0.0,
  strict). Asserts agent identity matches across baseline ↔ current.
  New `get_eval(eval_id)` storage method. 21 tests in `tests/test_baseline.py`.
- Per-case diff deferred to v0.4.1+ when datasets are big enough that
  aggregate isn't enough.

### Added — Run replay

- **`movate run <agent> --replay <run-id>`** (`core/run_replay.py` +
  `cli/run.py`) — single best regression-debug tool. Re-executes a
  recorded `RunRecord` through the *current* agent bundle (prompt, model,
  schemas, pricing all reload from disk; only the input is pinned).
  `AgentReplayDiff` surfaces `output_changed`, `status_changed`,
  `changed_keys` (top-level keys whose values diverge), cost delta, and
  latency delta. `-o json` for piping; `-o text` renders a Rich summary
  table to stderr with a slim diff JSON on stdout.
- Output changes are *not* failures — surfacing the diff IS the goal,
  exit 0 even when the agent now produces different output. Only a
  current-run error trips exit 1. Mismatch (run-id missing, agent name
  doesn't match the bundle) → exit 2.
- `--replay` is mutually exclusive with positional INPUT / `--input`.
  Workflow replay deferred; single-agent debug covers the 80% case.
  14 tests in `tests/test_run_replay.py`.

[0.4.0]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.4.0

## [0.3.1] — 2026-05-09

Patch release. Fixes a `RunRecord` double-save bug in `WorkflowRunner` that
would have inflated per-workflow cost / latency reports by ~2×. Surfaced by
a throwaway IR→LangGraph prototype (now deleted; findings preserved in
[docs/v0.3-langgraph-prototype.md](docs/v0.3-langgraph-prototype.md)).

### Fixed

- `Executor.execute(...)` now accepts `workflow_run_id` + `node_id` kwargs
  and stamps them onto the persisted `RunRecord`. `WorkflowRunner` no
  longer saves a second copy with a fresh UUID after every node.
- `WorkflowRunner._stamp_workflow_link` deleted. Per-node summaries are
  still synthesized in-memory for the `WorkflowResult.runs` view; on
  failure the runner persists a single `ERROR`-status row so
  `list_runs(workflow_run_id=…)` joins remain complete.

### Added

- [docs/v0.3-langgraph-prototype.md](docs/v0.3-langgraph-prototype.md) —
  four findings from the IR-vs-LangGraph seam check. Locks in the v1.1
  compiler design (`merge_dicts` state reducer, HITL via checkpointer,
  parallel-fan-out enum redundancy).

[0.3.1]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.1

## [0.3.0] — 2026-05-09

Sequential workflows: declarative `workflow.yaml` → IR → runner. Linear
chains of agent nodes only in v0.3; the IR is forward-aware (`NodeType` /
`EdgeKind` enums include conditional, parallel, HITL, sub-workflow variants)
so v1.1's LangGraph compiler can target it without a schema break.

**42 new tests** (196 unit + 3 smoke = 199 total). `ruff format`,
`ruff check`, `mypy src` (strict) all clean.

### Added — Workflow IR + compiler

- `WorkflowSpec` Pydantic model for `workflow.yaml` ([src/movate/core/workflow/spec.py](src/movate/core/workflow/spec.py)) — `kind: Workflow`, `state_schema`, `entrypoint`, `nodes`, `edges`. Node `type` constrained to `Literal["agent"]` so typos fail at parse time.
- `WorkflowGraph` IR ([src/movate/core/workflow/ir.py](src/movate/core/workflow/ir.py)) — nodes, edges, topology helpers (`successors`, `predecessors`, `sources`, `sinks`, `is_linear`, `topological_order`). Future-aware enums: `NodeType` ∈ {AGENT, TOOL, HUMAN, FUNCTION, SUB_WORKFLOW}, `EdgeKind` ∈ {SEQUENTIAL, CONDITIONAL, PARALLEL_FAN_OUT, PARALLEL_FAN_IN}.
- Two-pass compiler ([src/movate/core/workflow/compiler.py](src/movate/core/workflow/compiler.py)):
  - `compile_workflow(spec, dir)` — structural validation: duplicate ids, dangling edges, self-loops, cycles, orphan reachability, state-schema parsing.
  - `validate_linear(graph)` — v0.3 phase gate. Rejects branches, joins, conditional edges, non-agent node types with phase-aware error messages naming when each feature lands.

### Added — Workflow runner + storage

- `WorkflowRunner` ([src/movate/core/workflow/runner.py](src/movate/core/workflow/runner.py)) walks the IR in topological order. State plumbing: initial state validated against `state_schema`; per node, state is *projected* onto the agent's input schema (keys in `properties` only) and the agent's output is shallow-merged back. Mid-pipeline failures stop the run, retain the pre-merge state, and stamp the failed `node_id`.
- `WorkflowStatus` enum + `WorkflowRunRecord` Pydantic. `RunRecord` extended with optional `workflow_run_id` + `node_id` so per-node history joins back to the parent run.
- New sqlite `workflow_runs` table + idempotent `ALTER TABLE runs ADD COLUMN` migrations for existing v0.2 DBs.
- `InMemoryStorage` (in `movate.testing`) updated to match the new protocol.

### Added — Workflow CLI integration

- `is_workflow_path()` auto-detects workflow vs agent by presence of `workflow.yaml`.
- `movate validate <path>` — workflow branch prints topology chain, exits 0/2.
- `movate show <path>` — workflow branch renders Rich tables + ASCII chain + Mermaid `flowchart LR` block (paste into a PR for a live diagram).
- `movate run <path>` — workflow branch parses `INPUT` as JSON / file / stdin (no auto-wrap), executes through `WorkflowRunner`, prints per-node summary + `final_state`. `--output {table|json}`.

### Architecture decisions locked

- **IR is the contract; validators are policy.** The IR's enum members include v1.1+ variants. The compiler validators decide which variants are allowed *per phase*. v1.1 swaps `validate_linear` for `validate_dag` (and adds a `LangGraphCompiler` emitting LangGraph from the same `WorkflowGraph`) without touching the IR or the structural compiler.
- **State plumbing v0.3 = projection + shallow merge.** Explicit `inputs:` / `outputs:` mappings deferred to v0.4 when real workflows demand finer control.

[0.3.0]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.0

## [0.2.0] — 2026-05-08

First tagged release. Single-agent loop is production-ready; eval + bench
engines support exact-match and LLM-as-judge with cross-family enforcement;
`movate.testing` package is the consumer-facing test surface.

**157 tests** (154 unit, 3 live-API smoke). `ruff format`, `ruff check`, `mypy
src` (strict) all clean.

### Added — Agent loop (Phase 1 / v0.1)

- Typer + Rich CLI grouped by intent (`Develop`, `Run & evaluate`, `Diagnose`,
  `Deploy & operate`, `Manage`).
- `agent.yaml` schema (`api_version: movate/v1`) — Pydantic-validated; rejects
  floating tags, bad semver, wrong api_version, extra fields.
- Loader → `AgentBundle` (YAML + Jinja2 prompt + I/O JSON Schemas + sha256
  prompt hash).
- Linear executor with typed retry policy + provider fallback chain.
- `BaseLLMProvider` Protocol behind which the LiteLLM adapter lives — direct
  LiteLLM imports are forbidden in user code.
- `LiteLLMProvider` with typed exception mapping (auth, rate-limit, timeout,
  context-length, content-filter, model-unavailable). LiteLLM's own retry
  layer disabled (`num_retries=0`) so movate's policy owns retries.
- `MockProvider` — deterministic, network-free; judge-aware (returns a JSON
  score when the prompt contains `Rubric:`).
- Versioned pricing table at `providers/pricing.yaml`; cost-drift detection
  vs LiteLLM's reported cost (>5% logs loud).
- Per-run budget enforcement (`max_cost_usd_per_run` aborts with a typed
  error before billing).
- SQLite storage for runs, failures, and evals; idempotent `init()`.
- Stdout tracer (JSON-on-stderr, OTel-shaped span schema).
- Commands: `init`, `validate`, `show`, `run`, `doctor` (with API-key,
  pricing, and config presence checks).
- Agent template (`movate init`) ships with prompt, schemas, and a 2-case
  eval dataset.

### Added — Evals & bench (Phase 2 / v0.2)

- **Eval engine** (`movate.core.eval`) — dataset loader (sha256-stamped),
  judge config loader (auto-discovers `<agent>/evals/judge.yaml`), exact-match
  + LLM-as-judge scorers, N runs per case, aggregation modes
  (`mean | min | p10`).
- **Cross-family enforcement** (`assert_cross_family`) — Azure OpenAI is
  treated as the same family as OpenAI (shared weights → shared blind spots).
  Configs that share a family between agent and judge are rejected at
  parse time, not run time.
- **`movate eval`** — `--gate`, `--gate-mode`, `--runs`, `--mock`,
  `-o table | json | markdown`. Persists `EvalRecord` to sqlite for v0.4
  baseline diffing.
- **`movate bench`** — multi-model comparison with `--model` (repeatable)
  and `--judge`. Reuses `Executor.execute(model_override=…)` so each row
  tests exactly one model with no fallback contamination. Cross-family
  judge skips per-row with a stderr note rather than failing the whole
  bench. Reports cost (mean), latency (p50, p95), aggregated score, errors,
  and a sample output per model.
- **`movate pricing`** — Rich table + `-o json` + `-p <prefix>` filter.
- **Markdown reporter** — `render_eval_markdown` + `render_bench_markdown`
  in `movate.core.reporters`. GFM-safe (pipe-escape, backtick → `&#96;`,
  60-char input truncation). Suitable for `gh pr comment -F -`.

### Added — Templates

- Three new packaged templates beyond `default`:
  - **`faq`** — `{question}` → `{answer, confidence}`. Ships with
    `judge.yaml.example` (semantic-correctness rubric).
  - **`summarizer`** — `{text, max_words}` → `{summary, word_count}`. Ships
    with `judge.yaml.example` (faithfulness/coverage/brevity rubric).
  - **`classifier`** — `{text, labels[]}` → `{label}`. Exact-match-friendly
    (finite label set; no judge needed).
- Template registry at `src/movate/templates/__init__.py` — `TEMPLATES`
  dict + `get_template_path()` + `list_templates()`. New templates =
  drop a directory + add a line.
- `movate init -t {default|faq|summarizer|classifier}`.

### Added — `movate.testing` (consumer surface)

- Public package at [`src/movate/testing/`](src/movate/testing/):
  - **Doubles** — `InMemoryStorage` (StorageProvider conformance with
    filters), `NullTracer` (capture spans + events), `JudgeStubProvider`
    (splits agent vs judge prompts; captures bodies for assertion),
    re-exported `MockProvider`.
  - **Scaffolding** — `scaffold_agent(dst, name, template=…)` clones a
    packaged template; `build_test_executor(...)` wires test doubles into
    a ready-to-use Executor.
  - **Pytest fixtures** — `mock_provider`, `in_memory_storage`,
    `null_tracer`, `pricing`, `temp_agent_dir`, `build_executor`.
    Activate by adding `pytest_plugins = ["movate.testing.fixtures"]`
    to a consumer's `conftest.py`.

### Added — CI + smoke

- `.github/workflows/ci.yml` runs ruff, mypy, and `pytest -m "not smoke"`
  on every PR.
- Live-API smoke at `tests/test_smoke_litellm.py` — 3 tests (OpenAI direct,
  Anthropic direct, full executor against real OpenAI), gated by both
  `MOVATE_SMOKE=1` and the relevant API key. CI excludes the marker.
- `scripts/smoke.sh` — one-command invocation that sources `.env` and
  reports key presence before running.

### Architecture decisions locked

- **No LangGraph in v0.x.** Workflow orchestration (Phase 3 / v0.3) ships
  on a homegrown `WorkflowGraph` IR; LangGraph slots in as an alternative
  compiler at v1.1+ when conditional routing / parallel / HITL / checkpointing
  are actually needed.
- **LiteLLM is implementation detail.** All user code goes through
  `BaseLLMProvider`. The seam keeps room for v1.1+ provider routing.
- **Pricing is canonical, not inferred.** LiteLLM's reported cost is logged
  for drift detection but never used for billing.

[0.2.0]: https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.2.0
