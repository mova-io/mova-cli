# ADR 019 — Distributed trace-context propagation across the async job queue

**Status:** Accepted — shipped (trace-context propagation; tracing/log_correlation). _(status reconciled to shipped reality 2026-06-08)_
**Date:** 2026-05-24
**Deciders:** Engineering (observability + runtime)
**Context window:** v1.0 Azure operability — one distributed trace for an async run
**Related / constrained by:** ADR 001 (cloud-portability — W3C TraceContext is the vendor-neutral propagation standard, OTel is the blessed path), ADR 015 (self-hosted observability — App Insights / OTLP sink this trace lands in),
`src/movate/tracing/` (`base.Tracer` Protocol, `otel.py`, `__init__.build_tracer`, the new `propagation.py`),
`src/movate/core/models.py` (`JobRecord`), `src/movate/runtime/app.py` (enqueue handlers), `src/movate/runtime/dispatch.py` (`WorkerDispatch.execute_job`), `src/movate/storage/{postgres,sqlite}.py` + the `InMemoryStorage` double

---

## Decision

Propagate the **W3C trace context** through the job record so an async run is
**one distributed trace** end-to-end (`submit → queue-wait → claim → execute →
result`) instead of two disconnected ones. Specifically:

1. **(D1) Carry the context on the job, not in process memory.** Add a
   `trace_context: dict[str, str]` field to `JobRecord` — the standard W3C
   carrier (`traceparent` / `tracestate`). It is the durable bridge across the
   API pod → queue → worker pod hop; nothing ambient survives that hop.
2. **(D2) Inject at the enqueue edge.** At each `JobRecord` construction site in
   the API (`runtime/app.py`), stamp `trace_context=inject_current_trace_context()`.
   The helper returns the active span's carrier, or `{}` when no span is active /
   OTel is off. Population is **explicit at the edge** — the field's default is a
   plain empty dict (no context-capturing `default_factory`, no implicit magic).
3. **(D3) Continue in the worker.** `WorkerDispatch.execute_job` wraps the whole
   execution in `with continue_trace_context(job.trace_context):`. The
   `OtelTracer` starts its top-level span (`agent.execute` / the workflow root)
   against the **ambient current context**, so re-attaching the carrier makes
   the propagated span the parent — the execution spans nest under the
   originating trace.
4. **(D4) Vendor-neutral, OTel-optional, fail-soft.** Propagation is the
   **standard W3C TraceContext** via `opentelemetry.propagate` — no
   Azure-specific code (ADR 001). All of it lives in one module,
   `tracing/propagation.py`, behind a lazy/guarded import; with the `otel` extra
   off (or no active span) every helper is a complete no-op (`inject → {}`,
   `attach → None`, `continue_trace_context` does nothing) and **never raises**.

In one sentence: **the originating trace rides through the queue on the job
record — injected at enqueue, re-attached in the worker — so an async run is
one distributed trace, with a complete no-op when OTel isn't active.**

---

## Context

The runtime emits OTel spans, and ADR 015 wired an OTLP sink (Azure App
Insights). But a job's lifecycle spans **two processes**: the API enqueue
handler runs in its own trace, and the worker that later claims and executes the
job starts a **fresh root trace**. In the APM you therefore cannot see one job
as a single distributed trace — submit and execute are unconnected, so
queue-wait time, the claim, and the actual run can't be correlated to the
request that asked for them.

The fix is the **standard solution** for crossing an async boundary: serialize
the W3C trace context into the message (here, the job row) at the producer and
continue it at the consumer. The seam is already favorable — the `OtelTracer`
top-level span uses the ambient context as its implicit parent, so a re-attach
in the worker is enough; no executor/tracer API surgery is needed.

| Force | Weight |
|-------|--------|
| **One distributed trace** — operators can follow submit → queue → execute → result | HIGH |
| **Vendor-neutral (ADR 001)** — W3C TraceContext, no cloud-specific code | HIGH |
| **Back-compat** — pre-R2 rows and OTel-off deployments behave exactly as today | HIGH |
| **Boundary discipline** — storage stores a `dict[str,str]`, never imports OTel; propagation is wired only at the edges | HIGH |
| **Fail-soft** — propagation must never break enqueue or execution | HIGH |

---

## Decisions in detail

### D1 — `JobRecord.trace_context`
A `dict[str, str]` (default `{}`). Empty means "no propagated parent" — a pre-R2
row, or a job enqueued with OTel off → the worker starts a fresh root span
(today's behavior). The default is an empty dict, **not** an
ambient-capturing `default_factory`: capture is explicit at the enqueue edge so
constructing a `JobRecord` anywhere else (tests, the scheduler, triggers) stays
side-effect-free.

### D2 — Storage round-trip (additive, idempotent)
- **postgres:** `ALTER TABLE jobs ADD COLUMN IF NOT EXISTS trace_context JSONB;`
  (alongside the other `jobs` ALTERs). `save_job` persists the dict via the
  pool's JSON codec; `_row_to_job` reads it back, NULL → `{}`.
- **sqlite:** `ALTER TABLE jobs ADD COLUMN trace_context TEXT` in `_MIGRATIONS`
  (the duplicate-column guard keeps it idempotent on re-run); stored as JSON
  text, parsed back with a tolerant decoder, NULL/malformed → `{}`.
- **in-memory double:** stores the model directly — the field round-trips with
  no code change.
- Storage **never imports OpenTelemetry**: it stores/loads a plain
  `dict[str, str]`. This is purely an additive, nullable column — pre-R2 rows
  read back as `{}`.

### D3 — Propagation helpers (`tracing/propagation.py`)
The **only** place OTel context/propagation is touched, behind a guarded
`import opentelemetry.propagate / .context` (ImportError → feature-off):
- `inject_current_trace_context() -> dict[str, str]` — the active span's carrier,
  or `{}` (no span / no OTel).
- `attach_trace_context(carrier) -> token | None` + `detach_trace_context(token)`
  — the raw attach/detach pair.
- `continue_trace_context(carrier)` — a context manager wrapping attach/detach so
  the token can't leak; the worker uses this.

### D4 — Where it's wired
- **Enqueue (edge):** all `JobRecord` construction sites in `runtime/app.py` —
  the agent-run / workflow-resume dispatch path (the submit→execute trace
  operators care about), plus the generic `/jobs`, batch, eval, bench, and
  threaded-run sites. One-liner each.
- **Worker:** `WorkerDispatch.execute_job` wraps every kind (agent / workflow /
  eval / bench) in `continue_trace_context(job.trace_context)`.
- Non-edge constructors (`core/triggers.py`, `core/scheduler.py`) are left
  unchanged — they default to `{}` (a fresh root), the correct behavior for a
  background-originated job, and keeping `core` decoupled from the tracing edge.

---

## Consequences

**Positive**
- One distributed trace per async run in App Insights / any OTLP backend —
  submit, queue-wait, claim, execute, and result correlate.
- Vendor-neutral (W3C TraceContext) — no Azure lock-in (ADR 001).
- Fully additive + back-compatible: pre-R2 rows and OTel-off deployments are
  byte-for-byte the old behavior.
- Boundary-clean: storage stays OTel-free; propagation is wired only at the
  edges.

**Negative / risks**
- **At-least-once interaction.** A reaped/retried job re-attaches the **same**
  originating context on every attempt, so each attempt's spans nest under the
  original submit trace. This is **acceptable and intended** — all attempts of
  one logical job belong to one trace; attempts are still distinguishable by
  `attempt_count` and per-attempt span ids. We explicitly do **not** mint a new
  per-attempt parent.
- A malformed/foreign carrier would simply fail to extract → fresh root (the
  helpers swallow extraction errors); no crash, just a missing link.
- Slight row growth (a small JSON object per job) — negligible.

**Net-new:** the `JobRecord.trace_context` field, the additive `jobs.trace_context`
column on both SQL backends, the `tracing/propagation.py` helpers (exported via
`tracing/__init__`), the enqueue-site injections, and the worker continuation.
**No new dependency** — `opentelemetry` is an existing optional extra; the core
path never imports it.

## Alternatives considered
- **A side table / message-broker headers for the context** — rejected: the job
  row already crosses the exact boundary we need; a parallel channel is more
  moving parts for no gain.
- **Re-mint a fresh parent per retry attempt** — rejected (D-risk): it would
  scatter one logical job across many traces, the opposite of the goal.
- **Ambient `default_factory` capture on the model** — rejected: hidden magic;
  constructing a `JobRecord` off the request path would silently capture (or
  fail to capture) context. Explicit inject-at-edge is clearer and testable.
- **Status quo (two disconnected traces)** — rejected: this is the exact gap the
  ADR closes.
