# CLAUDE.md — operating rules for AI-assisted changes

`movate-cli` (`mdk`) is a reusable **framework + runtime + CLI + Azure
deployment tooling** for building, evaluating, and deploying AI agents. It is
embedded in customer deliverables, so the dominant risk is **architectural
entropy**, not syntax. Optimize for *stable*, not just *clean*.

Read this every session. The detail lives in the canonical docs linked at the
bottom — this file is the short version.

## How to work here

1. **Architectural intent first.** For a non-trivial change, state the goal +
   constraints, then propose: approach → files impacted → migration/compat
   concerns → alternatives → blast radius. Get agreement *before* editing code
   in `storage`, `runtime`, `core`, `providers`, `credentials`, or `infra`.
2. **ADR before structural change.** Storage/schema, a new adapter seam,
   runtime API, deployment lifecycle, auth/security → write an ADR under
   `docs/adr/` (match the latest ADR's structure) first.
3. **One PR = one responsibility.** No bundling unrelated work.
4. **No opportunistic refactors.** Only touch files necessary for the change.
   If you find adjacent debt, *document it* (a task, or a note in
   `docs/architecture-principles.md`) — do not silently fix it.
5. **Preserve backward compatibility** unless explicitly told otherwise.
   Explicitly flag any change to: `agent.yaml`/`project.yaml` schema, public
   CLI flags or `--json` shapes, the `/api/v1` runtime API, storage schema,
   `MOVATE_*`/`MDK_*` env vars, or deploy behavior. Deprecate before removing.
6. **Respect the boundaries.** Control plane (`cli`) ⊥ execution plane
   (`runtime`); `core` depends on adapter *Protocols*, never a concrete backend;
   `kb` uses the `StorageProvider` Protocol, not `postgres`/`sqlite`; tracing is
   wired at the edges, never imported into execution logic.
7. **Extend via adapters, don't hardcode.** New model → `BaseLLMProvider`
   (`providers/`); new persistence/vector store → `StorageProvider`
   (`storage/`); new observability sink → `Tracer` (`tracing/`). If a change
   can't be a new impl behind an existing Protocol, it needs an ADR.
8. **Minimal dependencies.** Favor composable Python over framework sprawl. No
   new framework without a proven scaling need. A new shipped dep must be
   permissively licensed (`scripts/check_licenses.py`) and justified in the PR;
   heavy/optional ones go in an opt-in `pyproject.toml` extra.
9. **Tests first for behavior.** Define expected behavior + edge cases +
   failure modes, then implement to satisfy them.
10. **Think in failure modes.** Ask what happens on: API/timeout failure,
    embedding-model or schema drift, partial retrieval, tracer unavailable,
    partial Azure deploy, duplicate jobs. Enterprise AI fails operationally.
11. **Treat generated code as a draft** — especially async, auth, infra,
    queues, retries, concurrency, persistence, observability.

## Verify before you call it done

```
ruff check src tests && ruff format --check src tests
mypy src
pytest -m "not smoke"
python scripts/check_licenses.py --strict   # shipped-dep license gate
```

Versioning is CalVer (`YYYY.M.D.N`). **Bump it IN your PR** — run
`python scripts/bump_version.py` (updates `pyproject.toml` /
`src/movate/__init__.py` / `uv.lock` together) and commit the result, so the
new version lands when the merge queue merges the PR. No separate push to
`main` is involved. (ADR 059's *at-merge* `release-version.yml` was removed:
its bump commit pushed directly to `main`, which the org's protection wouldn't
grant a token for — it failed on every merge. Per-PR bump via the queue needs
no special token. If two open PRs collide on the version line, rebase the later
one + re-run the script.) Never skip hooks or use `--no-verify` unless asked.

## Canonical docs (source of truth — read, don't reinvent)

- [`docs/architecture-principles.md`](docs/architecture-principles.md) — the
  layer map, adapter seams, boundary rules, compat contracts (the long form of
  rules 4–8).
- [`docs/adr/`](docs/adr/) — accepted architectural decisions.
- [`docs/license-posture.md`](docs/license-posture.md) — dependency-license policy.
- `docs/v1.0-azure-design.md`, `docs/azure-movate-architecture.md`,
  `docs/v1.0-overview.md` — platform direction (human-owned).

You accelerate implementation **within** these boundaries; humans own the
architectural direction.
