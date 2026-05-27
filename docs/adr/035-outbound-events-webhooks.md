# ADR 035 — Outbound events + webhook subscriptions

**Status:** Accepted
**Date:** 2026-05-27
**Deciders:** Engineering + Deva (Movate)
**Builds on / related:** ADR 017 (inbound triggers + job queue + HMAC), ADR 016
(drift), ADR 031 (SSE), `notify/` (deploy/drift-specific notifications), the
`/api/v1` surface.

## Context
Inbound triggers exist (run an agent when X happens), and `notify/` sends
deploy/drift notifications to Teams/Slack — but there is **no general,
subscribable lifecycle-event system**. The Mova iO front end and customer
systems can't react to platform events ("a run finished", "an eval failed",
"drift detected", "a canary promoted") except by polling. This turns the
platform from "poll me" into "I'll tell you" — a major integration capability.

## Decision
- **D1 — Lifecycle event model.** Typed, tenant-scoped, persisted events emitted
  at the edges (tracing-like, never inside `core` execution logic):
  `run.completed/failed`, `eval.completed/failed`, `drift.detected`,
  `canary.promoted/rolled_back`, `ingest.completed`, `agent.published`. Each
  carries an `event_id` (for dedupe) + the relevant resource ids.
- **D2 — Webhook subscriptions.** `POST /api/v1/webhooks` (subscribe: target URL
  + event types + secret; `admin`). Delivery is **HMAC-signed** (reuse the
  inbound-trigger HMAC), **at-least-once** with retries + dead-letter (reuse the
  ADR-017 job queue), and a delivery log. Egress is allowlisted (SSRF guard).
- **D3 — Front-end realtime SSE.** `GET /api/v1/events/stream` (tenant-scoped,
  event-type filterable; `read`) so the browser front end gets push updates
  without polling — reuses the existing SSE/run-streaming infra.

## Consequences
**Positive:** the front end + customer systems react to platform events in
realtime; reuses the queue (reliable delivery), the HMAC machinery, and the SSE
infra — no new substrate.
**Risks:** SSRF to untrusted webhook URLs (egress allowlist + scheme/host
validation); retry storms (exponential backoff + dead-letter); ordering —
delivery is at-least-once + unordered, so consumers dedupe on `event_id`
(documented in the contract).

## Boundaries
Events emitted at the edges (executor/dispatch/deploy), not in `core` logic;
delivery via the worker queue; SSE via existing streaming. `cli ⊥ runtime`.

## Scope / rollout
3 PRs: **D1** event model + table + edge emission (foundational, low `app.py`
surface — buildable first, sign-off-free) → **D2** webhook subscribe + signed
delivery via the worker → **D3** SSE event stream. D2/D3 add `/api/v1` endpoints
(contract-tested).
