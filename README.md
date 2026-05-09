# movate

Declarative platform for building, evaluating, and deploying AI agents and workflows.

## Status

**Phase 0 — skeleton.** The CLI scaffold is in place; commands print "not implemented" until their target phase lands. See [PRD_starting.md](PRD_starting.md) for the full spec and the [implementation roadmap](../../.claude/plans/want-to-take-inspiration-stateful-swan.md) for phase-by-phase plans.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Quickstart

```bash
uv sync
uv run movate --help
uv run movate doctor
uv run movate init my-agent             # default template (echo)
uv run movate init my-faq -t faq        # FAQ assistant
uv run movate init my-sum -t summarizer # summarizer
uv run movate init my-cls -t classifier # text classifier
```

### Available agent templates

| `-t` value | What it does | Eval default |
|---|---|---|
| `default` | Minimal echo agent (string-in, string-out) | exact-match |
| `faq` | Question → answer + confidence | ships `judge.yaml.example` |
| `summarizer` | Text + max_words → summary + word_count | ships `judge.yaml.example` |
| `classifier` | Text + label list → chosen label | exact-match (label set is finite) |

## CLI shape

```
Develop          init, validate, show
Run & evaluate   run, bench, eval, logs, trace
Diagnose         doctor, pricing
Deploy & operate serve, worker, deploy
Manage           auth
```

## Development

```bash
uv sync
uv run ruff check src tests
uv run ruff format src tests
uv run mypy src
uv run pytest -m unit
```

### Live-API smoke (opt-in)

Real money. Skipped by default. Run before tagging a release:

```bash
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-...
bash scripts/smoke.sh           # auto-sets MOVATE_SMOKE=1
```

Each test is also independently gated on the relevant API key, so a
partial keyring still produces a useful result.

## Testing your own agent

Add this to your project's `conftest.py`:

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

See [src/movate/testing/](src/movate/testing/) for the full surface
(`MockProvider`, `JudgeStubProvider`, `InMemoryStorage`, `NullTracer`,
`scaffold_agent`, `build_test_executor`).

## License

Proprietary. Internal Movate use only.
