# `__SKILL_NAME__` — Python lint runner (ruff)

Runs `ruff check` against a Python file or directory and returns
findings as a structured list. Used by the `code-reviewer` demo
agent to inform its review with concrete linter output rather than
asking the model to imagine what a linter would say.

## Why ruff

- **Fast** — Rust-backed; sub-100ms on most files.
- **Single binary** — `pip install ruff` or `uv tool install ruff`.
- **Stable JSON output** — `--output-format json` returns a
  predictable schema.
- **Wide coverage** — bugbear, isort, pyflakes, pycodestyle, pylint
  subset. Good signal without nit overload.

## Schema

**Input:**
```yaml
path: string         # file or directory to lint
select?: array       # optional rule codes (e.g. ["E501", "F401"])
```

**Output:**
```yaml
findings: array      # each: {file, line, column, code, message, severity}
ruff_version: string
warning?: string     # set on failure
```

## Severity bucketing

- `error` — E (pycodestyle errors), F (pyflakes), B (bugbear)
- `warning` — W (pycodestyle warnings)
- `info` — everything else (style / convention rules)

The bucket lets the agent rank findings without an embedded code-
table; ruff's full taxonomy is documented at https://docs.astral.sh/ruff/rules/.

## Cost + side effects

- `per_call_usd: 0.0` — local subprocess.
- `side_effects: read-only` — ruff reads files + reports findings;
  it doesn't modify anything. (The skill DOES exec a subprocess,
  but the side-effects enum captures DATA mutation, not process
  boundaries. Block subprocess exec via the skill-registry policy
  or a runtime sandbox.)

## Requirements

`ruff` must be on PATH. Install:

```bash
pip install ruff
# or
uv tool install ruff
```

If missing, the skill returns an empty findings list + a warning
field — the agent sees the error rather than crashing.

## Containerized / sandboxed alternative

For remote agents or hardened envs where subprocess exec isn't
allowed, swap this `impl.py` for one that POSTs the file to a
sandboxed lint service. The input + output schema is stable, so
the agent doesn't change.

## Testing

```bash
mdk skills run __SKILL_NAME__ --input '{"path": "src/movate/__init__.py"}'
```
