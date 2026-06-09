# ADR 083 — HITL Notifier delivery adapter (the escalation hand-off)

**Status:** Accepted — shipped (HITL notifier delivery adapter incl. the Slack sink; core/notifier_sinks). _(status reconciled to shipped reality 2026-06-08)_
**Date:** 2026-06-06
**Deciders:** Engineering (Movate)
**Implements:** ADR 077 D3 (the "delivery adapter" the durable-HITL rollout named
but didn't ship).
**Builds on / composes with (changes nothing in their wire contracts):**
ADR 062 (durable HUMAN node — the pause/resume core this notifies on),
ADR 017 D5 (native runner pause/resume — the second hook point),
ADR 057 (`AlertSink` Protocol + Teams/webhook adapters — the transport pattern
this mirrors), ADR 018 (BYOK — the operator brings their own webhook),
CLAUDE.md rules 6/7 (adapter seam, Protocol boundary, wired at the edge).

---

## Context

The durable-HITL core shipped: a workflow pauses at a HUMAN node, persists a
`PAUSED` `WorkflowRunRecord`, surfaces in `GET /api/v1/workflow-runs?status=paused`,
and resumes when `POST …/signal` arrives — on **both** the native runner
(ADR 017 D5) and the Temporal backend (ADR 062, `call_human_activity`).

The gap: **nobody is told a run is waiting.** The escalation just sits in the
paused inventory until someone polls it. ADR 077 D3 named the fix — "a
Teams/ServiceNow card behind a small `Notifier` Protocol carrying the screen-pop
context, so the 'escalate to a human' box is an actual hand-off" — but it was the
one part of the rollout left unbuilt. This ADR builds it.

## Decision

A small `NotifierProvider` seam, env-selected, fail-safe, fired from both
HUMAN-pause hooks — mirroring the shipped `AlertSink` (ADR 057) and
`build_dispatcher` patterns exactly.

### D1 — The seam (`core/notifier.py`)
A `NotifierProvider` Protocol with one method `async notify_human_pause(pause:
HumanPause) -> bool`, a `NoOpNotifier` default, and `build_notifier()` selecting
by `MOVATE_NOTIFIER` (`teams` / `webhook` / unset→no-op). A cached
`get_notifier()` singleton plus a one-liner `notify_human_pause_safe(pause)` that
execution logic calls. `HumanPause` is a frozen dataclass carrying only pause
metadata (run/workflow/node/prompt/output_contract/approvers/tenant/runtime) and
a `resume_url()` (the signal endpoint, prefixed by `MOVATE_RUNTIME_URL` when set).

### D2 — Concrete backends (`core/notifier_sinks.py`)
`TeamsNotifier` (a MessageCard with the prompt, run id, approvers, and the
decide-via URL) and `GenericWebhookNotifier` (a typed JSON envelope, optionally
HMAC-signed — same `X-MDK-Signature` scheme as `GenericWebhookSink`). Both are
**fire-and-forget**: a transport error / non-2xx logs and returns `False`; they
never raise. Imported **only** by `build_notifier` — never by execution logic, so
`core` depends on the Protocol, not a transport (rule 6/7). ServiceNow is a
future backend behind the same Protocol (deferred — add when a customer needs it).

### D3 — Two hook points, env-wired (no constructor threading)
The native runner's HUMAN-pause branch and the Temporal `call_human_activity`
each call `await notify_human_pause_safe(HumanPause(..., runtime="native"|"temporal"))`
**after** persisting the pause record. The notifier is resolved from the cached
env-selected singleton, so it's wired by configuration alone — no
`WorkflowRunner.__init__` / `configure_activities` signature change, no runtime
edge plumbing. Default (`MOVATE_NOTIFIER` unset) → `NoOpNotifier` → the native
and Temporal paths are byte-for-byte unchanged. This mirrors how metrics' module
accessors are called from execution logic.

### D4 — Approvers on the native pause record (parity)
The Temporal pause already stored `approvers` in `human_task`; the native runner
now does too (read from the HUMAN node metadata). Both backends therefore notify
with the same shape, and the paused-run inventory carries approvers regardless of
backend. This is an **additive** key on the `human_task` dict.

## Consequences

**Positive**
- The HUMAN node becomes a real hand-off: paused runs actively notify approvers.
- Backend-agnostic — one seam, both native + Temporal, identical payload.
- Zero behavior change when unconfigured; fail-safe (a notifier outage or
  misconfig never blocks a paused run — the record is the source of truth).
- New backends (ServiceNow, Slack, PagerDuty) are a new impl behind the Protocol.

**Negative / trade-offs**
- The notifier target is **global** runtime config (one Teams/webhook per
  deployment), not per-tenant or per-workflow. Per-workflow routing (the
  `approvers` list → specific channels) is a future enhancement; today approvers
  are surfaced *in* the notification, not used to *route* it.
- No timeout/re-notify/auto-escalation policy yet (ADR 077 D3 also lists it) —
  deferred to a follow-on; this ADR delivers the delivery adapter only.
- Authorization of *who* may resolve a pause (enforcing the `approvers` list at
  the signal endpoint) is out of scope — a separate follow-on.

## Alternatives considered
- **Thread a `NotifierProvider` through `WorkflowRunner.__init__` +
  `configure_activities`.** More explicit, but touches every construction site
  for no functional gain — the env-selected singleton (like `build_dispatcher` /
  metrics accessors) wires it with zero plumbing. Rejected for churn.
- **Reuse `AlertSink` (ADR 057).** It's shaped for operational alerts (drift,
  budget, SLO), not per-run approval hand-offs with approver/decision context.
  Mirrored its transport code; kept a distinct Protocol for a distinct purpose.
- **Notify from the signal/inventory API instead of the pause hook.** The pause
  happens in the worker/runner, not the API; hooking the pause is where the
  context is and works for both backends without the API in the loop.

## Compatibility (CLAUDE.md rule 5)
Additive. New module + Protocol; new env vars (`MOVATE_NOTIFIER`,
`MOVATE_NOTIFIER_TEAMS_WEBHOOK_URL`, `MOVATE_NOTIFIER_WEBHOOK_URL`,
`MOVATE_NOTIFIER_WEBHOOK_SECRET`). One additive key (`approvers`) on the
`human_task` dict in the `PAUSED` record — readers of `prompt`/`output_contract`
are unaffected; the Temporal path already carried it. No change to the `/api/v1`
request/response shapes, storage schema, CLI flags, or execution semantics. The
native + Temporal paths are byte-for-byte unchanged when `MOVATE_NOTIFIER` is unset.
