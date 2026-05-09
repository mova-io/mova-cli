# movate

Declarative platform for building, evaluating, and deploying AI agents and workflows.

**Internal Movate framework.** Proprietary; private artifact distribution
only — see [RELEASING.md](RELEASING.md). Public PyPI is intentionally
not used.

## Status

| version | tag | what landed |
|---|---|---|
| 0.4.0 | [`v0.4.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.4.0) | Observability + regression-detection (Langfuse, OTel, trace replay, eval baseline diff, run replay, CI eval-gate) |
| 0.3.1 | [`v0.3.1`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.1) | Workflow runner double-save fix |
| 0.3.0 | [`v0.3.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.3.0) | Sequential workflows (forward-aware IR + compiler + runner) |
| 0.2.0 | [`v0.2.0`](https://github.com/jeremyyuAWS/movate-cli/releases/tag/v0.2.0) | Eval engine (exact-match + LLM-as-judge with cross-family enforcement) |

**v0.5 in progress (`main`)** — service mode. Stages 1-3a shipped:
job-queue data layer, API key auth, FastAPI runtime with `/healthz`,
`POST /run`, `GET /jobs/{id}`. Stages 3b/4/5 (`/agents`, worker,
PostgresProvider) coming next. Design decisions locked in
[docs/v0.5-design.md](docs/v0.5-design.md).

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
| HTTP runtime | `movate serve` | ⚠️ stage 3a wired, stage 3b ships CLI binding |
| Background worker | `movate worker` | ⚠️ stub; lands stage 4 |
| Postgres backend | (auto via env) | ⚠️ lands stage 5 |
| Azure deploy | `movate deploy` | ⚠️ stub; v1.0 |

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
