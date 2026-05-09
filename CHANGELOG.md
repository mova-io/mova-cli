# Changelog

All notable changes to movate. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows [SemVer](https://semver.org/).

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

[0.3.0]: https://github.com/movate/movate-cli/releases/tag/v0.3.0

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

[0.2.0]: https://github.com/movate/movate-cli/releases/tag/v0.2.0
