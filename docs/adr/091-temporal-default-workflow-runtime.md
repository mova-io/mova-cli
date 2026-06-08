# ADR 091 — Temporal as the default workflow runtime (graceful)

Status: Accepted
Date: 2026-06-08
Deciders: Engineering + Deva (Movate)
Builds on: ADR 054/055 (Temporal backend), ADR 062 (durable HITL), ADR 089
(non-blocking dispatch).

## Context

Durable, deterministic, observable execution is the platform's headline
workflow story (ADR 054/055/062). Yet `runtime` has defaulted to **native**
(in-process `WorkflowRunner`) since the backend shipped opt-in
([spec.py] `WorkflowSpec.runtime`, [ir.py] `WorkflowGraph.runtime`). Deva
approved making **Temporal the default workflow engine** — durability should be
what you get unless you opt out, not what you opt into.

The constraint: Temporal is an **optional** backend — the `[temporal]` extra
plus a reachable `TEMPORAL_HOST`. A blunt `default = "temporal"` would make every
unspecified workflow hard-require a Temporal server, breaking local `mdk run`,
CI, and any deploy without Temporal. Two node types (`FUNCTION`, `SUB_WORKFLOW`)
also raise `NotImplementedError` on the Temporal compiler today, so some valid
native workflows can't run there yet.

## Decision

Make Temporal the default **gracefully**: a new `auto` runtime (the new default)
resolves to Temporal **when it can actually run there**, otherwise native. The
explicit values keep their exact meaning.

### D1 — `auto` is the new default runtime

`WorkflowSpec.runtime` and `WorkflowGraph.runtime` default to `"auto"` (added to
the `Literal` + `VALID_RUNTIMES`). A workflow that *omits* `runtime:` is now
`auto`.

### D2 — `auto` resolves Temporal-first, native-fallback

`resolve_effective_runtime(graph, override)` resolves `auto` to:

```
temporal   if  _temporal_available()  AND  _temporal_compilable(graph)
native     otherwise
```

- `_temporal_available()` — **non-throwing** probe: the `[temporal]` extra
  imports AND `TEMPORAL_HOST` is configured. (The sibling `require_backend_*`
  throwers are unchanged — used only after resolution.)
- `_temporal_compilable(graph)` — no node the Temporal compiler can't emit
  (`FUNCTION`, `SUB_WORKFLOW` today). Keeps an `auto` workflow that uses those
  nodes on native instead of failing.

### D3 — Explicit `runtime:` is unchanged (fail-loud preserved)

`runtime: temporal` still **fails loud** when Temporal is unavailable — an
explicit ask for durability must NOT silently degrade (ADR 055 D6). `auto` is the
only value that falls back. `runtime: native` is honored verbatim. So the only
behavior change is for workflows that previously *relied on the implicit native
default* — and they only move to Temporal when it can run them, byte-for-byte
native otherwise.

### D4 — One resolution, everywhere

The Temporal worker's registration filter ([cli/worker.py]) and the dispatch
fork ([dispatch.py]) both route through `resolve_effective_runtime(g, None)`
(not a raw `g.runtime == "temporal"` check), so an `auto`-resolves-to-Temporal
workflow is hosted by the worker and dispatched durably — consistently.

## Consequences

**Compat / blast radius (rule 5):** the `runtime` Literal gains `auto` (additive,
`extra="forbid"` still holds — old workflow.yaml files omit the key and read
`auto`). No `/api/v1` shape, CLI flag, env var, or storage change. Where Temporal
is unconfigured (local dev / CI), `auto` is **byte-for-byte the old native
path** — existing tests that ran native still do. Where Temporal IS configured
(the demo/prod runtimes), unspecified workflows now run durably by default — the
intended change. `--runtime native` / `--runtime temporal` overrides unchanged.

**Why graceful, not hard:** a hard default would force Temporal into every
environment and break the `FUNCTION`/`SUB_WORKFLOW` workflows immediately. The
graceful default delivers "durable by default where it counts" with zero
breakage, and the `FUNCTION`/`SUB_WORKFLOW`-on-Temporal gap can close
independently (then those workflows auto-upgrade with no spec change).

**Follow-on (out of scope):** `FUNCTION`/`SUB_WORKFLOW` Temporal emission;
surfacing the resolved backend in `mdk workflow lint` / run output so operators
see "auto → temporal/native (reason)".

## Verification

```
ruff check src tests && ruff format --check src tests
mypy src
pytest -m "not smoke" tests/test_workflow_runtime_resolution*.py \
       tests/test_temporal_compiler.py tests/test_temporal_execution.py
pytest -m "not smoke"            # full suite — native fallback keeps it green
```

- New tests: `auto` → temporal when available+compilable; → native when the
  extra is absent / `TEMPORAL_HOST` unset / a `FUNCTION` node is present;
  explicit `temporal` still fails loud unavailable; explicit `native` honored.
