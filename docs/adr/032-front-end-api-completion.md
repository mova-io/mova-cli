# ADR 032 — Front-end API completion: draft/preview authoring, aggregate monitor, async KB ingest

**Status:** Accepted
**Date:** 2026-05-27 (proposed); 2026-05-27 (approved)
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x — close the last `/api/v1` gaps the Mova iO front end needs
to drive the platform over HTTP (curl from Azure).
**Builds on / related:** ADR 013 (scopes/least-privilege), ADR 014 (durable
registry), ADR 016 (eval/drift), ADR 017 (job queue + KEDA worker), ADR 023
(auto-RAG), ADR 031 (reporting/dashboards), the `/api/v1` runtime surface, the
front-end API audit (`docs/front-end-api.md` + `tests/test_front_end_api_contract.py`).

## Context

The front-end API audit confirmed the runtime `/api/v1` already covers `add`,
`validate`, `deploy` (agent-level publish/canary), and `monitor` (per-entity
run→trace→explain→eval) for the Mova iO front end, with per-endpoint scopes and
an OpenAPI contract test. Three front-end-facing gaps remain:

1. **No server-side LLM authoring + no draft/preview.** Every create endpoint is
   structured-fields / pre-built-bundle only (`WizardAgentSubmission.agent_prompt`
   is the literal prompt template, written verbatim — no model call); the
   `mdk init --llm` generator is reachable from the CLI but not the runtime. And
   validation only happens as a side effect of `POST /agents` (which `409`s on an
   existing name) — so there's no "describe in English → preview" and no "lint
   before I commit".
2. **No aggregate monitor data API.** A dashboard must page raw `/runs`; the
   `mdk report` rollup (trends/cost/latency/top-failures) is CLI-only.
3. **KB ingest is synchronous.** `POST /kb` chunks+embeds+inserts in-request;
   there is no `JobKind.INGEST`, so bulk/URL-crawl ingest risks request timeouts
   and doesn't ride the KEDA-autoscaled worker.

## Decision

Add three **additive, scope-gated, OpenAPI-documented** `/api/v1` capabilities,
each reusing shipped substrate. `/api/v1` is a compat contract (CLAUDE.md rule
5): all additions are versioned and covered by the audit's contract test.

### D1 — Draft/preview + LLM authoring create
`POST /api/v1/agents/preview` (scope `admin`): accept **either** a free-text
`description` (LLM-generate) **or** a candidate bundle, then **generate and/or
validate** it and return the candidate (`agent.yaml` + `prompt.md` + schemas +
seed eval cases + validation report + cost forecast) **without persisting**. The
front end then commits via the existing `POST /agents`.
- **Boundary work:** the generator is `movate.scaffold.generate_agent_from_description`
  (a non-`cli` module). Confirm it has zero `cli` coupling (factor out any that
  exists) so the runtime can import it; call it through the existing provider
  seam (BYOK key). `cli ⊥ runtime` preserved — the runtime never imports `cli`.
- **Scope discipline:** preview = generate + validate **only** (fast,
  synchronous). It does NOT crawl/ingest a KB — that's D3's async path. Keeps the
  request fast and failure modes simple. A separate `/agents/preview` endpoint
  (not a `dry_run` flag on `POST /agents`) keeps "produce a candidate without
  persisting" cleanly distinct.

### D2 — Aggregate monitor endpoints
`GET /api/v1/report` (cross-agent) + `GET /api/v1/agents/{name}/metrics`
(per-agent), scope `read`: pass-rate trends, cost-over-time, latency p50/p95/p99,
top failing cases, per-agent/workflow rollups — JSON the front end renders.
- **Boundary work:** the rollup logic lives in `cli/report_cmd.py` today (the
  runtime can't import `cli`). **Factor the pure aggregation into a
  backend-agnostic module** (e.g. `core/reporting.py`) that BOTH `mdk report` and
  the endpoint call. The CLI must keep working — both call-sites tested.
- Complements ADR 031's Grafana/Azure dashboards (ops) + Langfuse (LLM
  observability): this is the in-product, no-external-infra monitor feed.

### D3 — Async KB ingest
Add **`JobKind.INGEST`**: `POST /api/v1/agents/{name}/kb/ingest` (scope
`kb:write`) **enqueues** a crawl+chunk+embed+insert job on the durable queue; the
KEDA worker processes it; the front end polls `GET /jobs/{id}` for progress (same
pattern as agent/workflow runs). Keep the existing synchronous `POST /kb` for
small/inline ingest (back-compat); the async path is for bulk / URL-crawl.
- **Reuses** the ADR-017 job substrate (queue + KEDA + retry/dead-letter +
  submission idempotency). Closes the storage scale gap — no request timeouts,
  progress, backpressure, horizontal scale.

## Consequences

**Positive**
- The front end gets describe→preview→commit authoring, an in-product monitor
  feed, and scalable bulk ingest — all reusing shipped pieces. `/api/v1` stays
  additive + versioned + contract-tested.
- Closes the audit's #1 gap (no LLM authoring / no draft create) and the storage
  scale gap (sync ingest) in one coherent decision.

**Negative / risks**
- The **cli→core refactors** (D1: confirm the generator is `cli`-free; D2:
  extract the aggregation into `core/reporting.py`) must not regress the CLI —
  shared module + both call-sites tested.
- D1 makes a **provider call inside an HTTP request** — bounded by "generate +
  validate only, no ingest"; needs a timeout + a clear error when no provider key
  is configured for the tenant.
- D3 is a **new `JobKind` + worker path** — reuse existing idempotency/replay;
  cover partial-ingest + worker-crash recovery.

## Alternatives considered
- **Front end shells out to the CLI** (SSH/subprocess): rejected — fragile for a
  hosted service; the runtime API is the contract.
- **`dry_run` flag on `POST /agents`** instead of a `/preview` endpoint: rejected
  — overloads create semantics; a distinct endpoint is clearer for the front end.
- **Sync-only ingest:** rejected for bulk (timeouts); we keep sync for small +
  add async for bulk.
- **Duplicate the rollup in the endpoint** instead of factoring it: rejected
  (drift between CLI and API).

## Scope / rollout
Three PRs (one per decision), independent lanes (authoring / monitor / kb) but
all touching `runtime/app.py`, so **sequence the endpoint additions** to avoid
conflicts. Each additive + contract-tested (extend the audit's
`test_front_end_api_contract.py`) + OpenAPI-documented. Suggested order:
**D2** (report — lowest risk, reuses the just-shipped `mdk report` aggregation) →
**D1** (preview/LLM — the biggest front-end win) → **D3** (async ingest — queue
work). D1+D2 land their `cli→core` refactor first.
