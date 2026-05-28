# ADR 033 — API hardening: production-grade robustness for the front-end `/api/v1`

**Status:** Accepted
**Date:** 2026-05-27 (proposed); 2026-05-27 (approved)
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x — make `/api/v1` production-grade for a browser front end
(Mova iO) at scale, uniformly rather than per-endpoint.
**Builds on / related:** ADR 013 (scopes), ADR 017 (jobs), ADR 024
(log-correlation), ADR 031 D1 (trace URLs), ADR 032 (front-end endpoints),
`runtime/errors.py` (error envelope), `runtime/middleware.py`, the front-end
audit's contract test.

## Context

The audit + a robustness review found the `/api/v1` foundation strong —
consistent error envelope (`{error:{code,message,request_id}}`), CORS, scopes,
`Idempotency-Key` on runs, per-tenant rate-limiting, async jobs, SSE, OpenAPI +
a contract test. To be production-grade for a browser front end at scale, harden
the cross-cutting + compat-sensitive properties **uniformly**.

## Decision

Add cross-cutting API hardening across `/api/v1`, mostly additive middleware +
a few compat-sensitive conventions, all covered by the contract test. The new
ADR 032 endpoints adopt these from birth.

- **D1 — Uniform cursor pagination.** Every list endpoint on `limit` (capped
  default) + opaque `cursor`/`next_cursor` + `has_more` via a shared helper.
  Existing `limit` callers unaffected; `cursor` additive. Caps stop unbounded
  payloads.
- **D2 — Request correlation.** A request-id middleware: honor inbound
  `X-Request-Id` or generate one, bind to the logging/trace context (ADR 024),
  emit `X-Request-Id` on **every** response (matching the envelope's
  `request_id`). Surface the Langfuse trace-URL header (ADR 031 D1) for traced
  runs.
- **D3 — Rate-limit signaling.** `429` + `Retry-After` +
  `X-RateLimit-Limit/Remaining/Reset` on the existing limiter so clients back
  off correctly.
- **D4 — Idempotency everywhere.** Extend the runs `Idempotency-Key` machinery to
  all unsafe mutations (create-agent, publish, promote, revert, dataset, ADR
  032's preview-commit + D3 ingest-enqueue) via a shared dependency.
- **D5 — Optimistic concurrency.** `ETag` on agent reads + `If-Match` on
  `PUT /agents/{name}` + publish/promote, backed by the registry version; `412`
  on a stale write. Rolled out **soft → enforced** (server returns `ETag`,
  accepts writes without `If-Match` but warns; require it once the front end
  adopts it) so it never breaks naive clients.
- **D6 — Payload limits.** Configurable max request-body size on uploads + ingest
  → `413` with the limit in the error envelope.
- **D7 — OpenAPI completeness.** Every endpoint declares `response_model` +
  documented error `responses={…}` + ≥1 example; the contract test asserts
  presence.
- **D8 — Versioning + deprecation policy.** Document the `/api/v1` stability
  contract; emit `Deprecation` + `Sunset` headers on any deprecation; the
  contract test guards removals.

## Consequences

**Positive** — a uniform, predictable contract the front end can rely on
(paginate, retry, correlate, back off, avoid lost updates), reusing the existing
envelope/limiter/registry-version/correlation context.

**Negative / risks** — D1/D4/D5/D8 touch the `/api/v1` contract; all additive
except D5's `If-Match`-required, which ships **soft-then-enforced** with a
migration window. Everything versioned + contract-tested.

## Boundaries
Cross-cutting middleware + per-endpoint conventions; `cli ⊥ runtime`; reuse (not
reinvent) the envelope/scopes/limiter/registry-version; correlation at the edge.

## Scope / rollout (layered PRs, low-risk first)
1. **Layer 1 — middleware bundle:** request-id (D2) + rate-limit headers (D3) +
   payload limits (D6). Additive, immediate front-end value.
2. Pagination standardization (D1 — shared helper + per-list adoption).
3. Idempotency-everywhere (D4 — shared dependency).
4. `ETag`/`If-Match` (D5 — soft→enforced).
5. OpenAPI completeness (D7) + versioning/deprecation policy (D8) + contract-test
   assertions.

Coordinated with ADR 032 — new endpoints born compliant.
