# ADR 037 — Workflow API parity: manage workflows over `/api/v1`, not just run them

**Status:** Accepted
**Date:** 2026-05-27
**Deciders:** Engineering + Deva (Movate)
**Builds on / related:** `core/workflow/` (`WorkflowSpec`, `WorkflowRunner`), ADR
014 (durable registry), ADR 024 (workflow spans), ADR 029 (workflow authoring),
the agent-lifecycle `/api/v1` endpoints.

## Context
Agent CRUD is rich over `/api/v1` (create/validate/version/publish/canary).
Workflow **execution** (`/workflow-runs`) + HITL **signal** are exposed, but
workflow **definition** management (create / update / version / publish a
`WorkflowSpec`) is CLI-only. So the Mova iO front end can *run* a workflow but
can't *build or manage* one — a parity gap with agents.

## Decision
- **D1 — Workflow CRUD parity.** `POST/GET/PUT/DELETE /api/v1/workflows` over the
  `WorkflowSpec` (nodes/edges/state_schema/entrypoint) + `POST
  /workflows/{name}/validate` (node-id uniqueness, edge endpoints exist,
  entrypoint exists, cycle sanity — reuse ADR 029 D4's verify) + `versions` /
  `history` / `publish` (reuse the durable registry, ADR 014). Mutations `admin`,
  reads `read`.
- **D2 — Run management parity.** List/get workflow runs, the existing HITL
  `signal`, and the per-node trace (ADR 024 workflow spans) — so the front end
  monitors a workflow run like an agent run.
- **D3 — Authoring over the API.** Expose ADR 029's workflow catalog actions (the
  plan → apply → verify spine) via the API so the front end *builds* workflows
  the same guided way it builds agents. Coordinate with ADR 029.

## Consequences
**Positive:** the front end gets full workflow lifecycle (build/test/version/run/
monitor) at agent parity, reusing `WorkflowSpec` + `WorkflowRunner` + the registry
+ ADR 029's authoring spine — no new engine.
**Risks:** workflow-integrity validation at the API edge (reuse ADR 029 D4 — don't
re-implement); the registry must treat workflows as a first-class versioned
entity alongside agents.

## Boundaries
Reuse `WorkflowSpec`/`WorkflowRunner`/registry/ADR-029 verify; `cli ⊥ runtime`;
`/api/v1` additive + versioned + contract-tested.

## Scope / rollout
**D1** (workflow CRUD + validate + versioning/publish) → **D2** (run management
parity) → **D3** (authoring-over-API, with ADR 029). All add `/api/v1` endpoints
(shared `runtime/app.py`) → sequence behind the other endpoint work to avoid
conflicts.
