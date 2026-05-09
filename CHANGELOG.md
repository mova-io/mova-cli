# Changelog

All notable changes to movate. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows [SemVer](https://semver.org/).

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
