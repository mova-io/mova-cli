# AGENTS.md — working ON `movate-cli`

This file is for contributors and AI coding agents (Claude Code, Cursor,
…) hacking on `movate-cli` (`mdk`) **itself**. `AGENTS.md` is the
tool-agnostic convention; it exists so non-Claude agents find their way
to the authoritative rules.

## Read `CLAUDE.md` first — it is the source of truth

The operating rules for any non-trivial change (architectural intent
first, ADR before structural change, one PR = one responsibility, no
opportunistic refactors, backward-compat contracts, layer boundaries,
the adapter seams) live in [`CLAUDE.md`](CLAUDE.md). Read it every
session. This file does not restate those rules — it points at them.

## Verify before you call it done

Run the full gate and get it green:

```
ruff check src tests && ruff format --check src tests
mypy src
pytest -m "not smoke"
python scripts/check_licenses.py --strict   # shipped-dep license gate
```

## Versioning is automatic

Versions are CalVer (`YYYY.M.D.N`) and bumped by `.githooks/pre-commit`
across `__init__.py`, `pyproject.toml`, and `uv.lock`. **Do not
hand-edit version files.** Never skip hooks or use `--no-verify` unless
explicitly asked.

## Worktree isolation

AI agents run in an isolated git worktree under `.claude/worktrees/`,
not the primary checkout. Confirm your root with
`git rev-parse --show-toplevel` before any `git` command, and operate
only from that worktree.

## Canonical docs

- [`CLAUDE.md`](CLAUDE.md) — the operating rules (authoritative).
- [`docs/architecture-principles.md`](docs/architecture-principles.md) —
  layer map, adapter seams, boundary + compat contracts.
- [`docs/adr/`](docs/adr/) — accepted architectural decisions.
- [`docs/license-posture.md`](docs/license-posture.md) — dependency-license policy.
