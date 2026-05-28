# ADR 045 — API ergonomics + next-generation capabilities

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x runtime API is **broad** (74 routes on the
`/api/v1` router, the "75+" surface) and already **consistent** — flat
least-privilege scopes (ADR 013), one error envelope (`runtime/errors.py`),
submission idempotency via `Idempotency-Key` keyed on
`(tenant_id, idempotency_key)`, an SSE run/event stream
(`runtime/app.py::_sse_run_stream`, ADR 035 D3), and symmetric rate-limit
headers (`runtime/middleware.py`). This ADR is the deliberate step from
**functional** to **best-in-class**: it (a) codifies eight cross-cutting
quality bars as **contract** that every endpoint meets going forward, and (b)
reserves the URL space + semantics for **five flagship next-gen capabilities**
so they land coherently instead of as ad-hoc bolt-ons.
**Builds on / depends on:**
ADR 013 (end-to-end identity — the flat scope model every decision here
declares against),
ADR 032 (front-end API completion — the describe/preview surface and the
front-end contract this generalizes),
ADR 035 (outbound events / webhooks / SSE — D3's tenant-scoped SSE stream;
the **event** stream that D11's **output** stream is deliberately kept
distinct from),
ADR 036 (usage metering + quotas — the per-tenant spend/quota substrate the
economics headers and semantic-cache budgeting read from),
ADR 040 (projects as a first-class cloud entity — the scope unit sessions,
replay, and feedback attribute to),
plus the **in-flight foundational endpoints** landing in parallel PRs tonight
(see *First implementations* below).
**Defining architectural fact:** MDK deploys **one runtime per customer
tenant**, each potentially on a **different CalVer version** with a
**different set of enabled extras**. A single front end (Mova iO) therefore
talks to **N heterogeneous runtimes** simultaneously. Several decisions below
(D7, D9 especially) exist *specifically* to make that tractable — the front
end must discover, not assume, what each runtime can do.

---

## Context

The API works. The gap is that "works" is not "best-in-class," and three
forces make the gap expensive to leave open:

1. **The functional-vs-best-in-class gap.** Today a caller can run an agent,
   poll a job, and read a result. What it *cannot* reliably do: learn the
   economic cost of the call it just made, recover from an error without
   reading prose, preview a mutation before committing it, project a sparse
   response, or discover the next valid action. These are the table stakes of
   a 2026 enterprise API, and they are *cross-cutting* — a one-off fix per
   endpoint guarantees drift. They have to be **contract**, applied uniformly
   and additively.

2. **Per-tenant version drift.** Because each tenant's runtime is its own
   CalVer build with its own extras, a front end cannot hardcode "this
   runtime supports semantic cache" or "this runtime has the sessions table."
   Two tenants on `2026.4.1.0` and `2026.5.28.0` expose different
   capabilities. Without a machine-readable way to *ask a runtime what it can
   do*, the front end either degrades to the lowest common denominator across
   the whole fleet or ships brittle per-tenant special-casing. D9 (Capability
   Discovery) and D7 (machine-readable changelog + deprecation signaling)
   exist to turn "guess and hope" into "ask and adapt."

3. **Economic transparency + self-description on a BYOK platform.** MDK is
   bring-your-own-key: the customer's provider spend flows through *their*
   keys, so they are acutely cost-sensitive and entitled to see, per call,
   what a request cost them. And because the platform is embedded in customer
   deliverables, the API must **teach its own use** — errors that point at
   docs and suggest a fix, responses that advertise their next actions, a spec
   clean enough to generate typed SDKs. A self-describing, economically
   transparent API is not a nicety here; it is the difference between an
   integration a customer's team can own and one that needs us in the room.

This ADR is **additive-only**. CLAUDE.md rule 5 (preserve backward
compatibility; the `/api/v1` runtime API is an explicit compat surface) is
**preserved absolutely** — see R1 and the constraint notes under D2.

### A note on the existing error envelope + header machinery (verified before specifying)

Two facts about today's implementation **constrain** the additive quality
bars below, and are called out so the implementation PRs don't trip on them:

- **The error envelope is narrower than the prose elsewhere implies.** Today
  `runtime/errors.py` defines the wire shape as
  `{"error": {"code": "...", "message": "..."}}` — **two fields, not three.**
  `request_id` is documented in the module docstring as a *planned* future
  expansion but is **not yet a field on the wire**. Crucially, both
  `ErrorBody` and `ErrorResponse` set `model_config = ConfigDict(extra=
  "forbid")`. That `extra="forbid"` is the real constraint: D2's additive
  fields (`docs_url`, `fix_hint`, `retriable`, `retry_after_ms`) **cannot**
  ride as loose extras — they must be added as **explicit `Optional` fields**
  on `ErrorBody`, and `request_id` must be promoted to a real (optional) field
  at the same time. Adding optional fields to an `extra="forbid"` model is
  back-compatible on the wire (omitting a `None` optional changes no existing
  consumer); silently relaxing to `extra="allow"` is **not** the chosen path.
  This is recorded as the first implementation note for D2.

- **Rate-limit headers are stamped in the auth dependency, not a true ASGI
  middleware.** `runtime/middleware.py::make_auth_dependency` writes
  `X-RateLimit-*` (and the additive `X-RateLimit-Tenant-*`) onto the injected
  `Response` object per request, and `errors.py::rate_limited` re-stamps them
  on the 429 path so the two are symmetric. There is **no** outer ASGI
  middleware that post-processes every response. This matters for D1: the
  economics headers (`X-MDK-Cost-USD`, etc.) follow the **same per-handler
  `Response.headers` stamping pattern**, computed where the cost is known (at
  the executor seam), not in a global middleware that would have to reach back
  into execution state — which would violate the boundary rule (tracing/cost
  is wired at the edges, never imported into execution logic). For the
  **streaming** surfaces (D11), where headers must flush before the body, the
  economics values that aren't known until the stream ends are emitted as a
  **trailing SSE `usage` frame** rather than a header. This is the one place
  the "headers carry economics" rule yields to the transport, and it is noted
  in D1 + R5.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Version-resilient front end** — one Mova iO talks to N heterogeneous runtimes; it must discover capabilities, not assume them (D9, D7) | HIGH |
| **Economic transparency on a BYOK platform** — every response says what it cost; customers own their spend (D1) | HIGH |
| **Self-teaching API** — errors point at docs + suggest fixes; responses advertise next actions; spec generates SDKs (D2, D6, D8) | HIGH |
| **Additive-only — back-compat is absolute** — no field/header/body removed or renamed; CLAUDE.md rule 5 preserved (R1) | HIGH |
| **Conversational + stateful CX** — server-managed sessions, output streaming, replay, feedback close the product gap vs. status-quo stateless calls (D10–D14) | HIGH |
| **Reserve URL space + semantics now** — flagships land coherently against a declared contract, not as drifting bolt-ons | HIGH |
| **Reuse existing seams** — CacheProvider for D12, eval+harvest infra (ADR 016) for D13/D14, SSE transport (ADR 035 D3) for D11, idempotency for D10 | MED |
| **Privacy: per-tenant isolation is non-negotiable** — semantic cache + sessions + feedback are tenant-partitioned, never cross-tenant (R4, R6) | HIGH |
| **Best-effort economics over wrong economics** — a cost we can't compute yet is an omitted header, never a fabricated value (R2) | MED |

---

## Architecture

```
                         Mova iO  (one front end)
                              │
        ┌─────────────────────┴──────────── … ────────────────┐
        ▼                     ▼                                ▼
  tenant A runtime      tenant B runtime               tenant N runtime
  CalVer 2026.4.1.0     CalVer 2026.5.28.0             CalVer 2026.5.10.2
  extras: {pg, sse}     extras: {pg, sse, semcache}    extras: {sqlite}
        │                     │                                │
        └── GET /api/v1/capabilities  ◄── the front end ASKS each runtime ──┘
                              │
   Part 1 (cross-cutting CONTRACT, every route, additive-only):
     D1 economics+rate headers · D2 self-teaching errors · D3 X-Dry-Run
     D4 wait= long-poll · D5 ?fields/?expand · D6 _links · D7 changelog/deprecation
     D8 codegen-clean OpenAPI → typed SDKs

   Part 2 (five flagships — reserved URL space + semantics):
     D9  GET  /api/v1/capabilities
     D10 POST /api/v1/sessions → /sessions/{id}/messages → GET /sessions/{id}
     D11 POST /api/v1/agents/{name}/runs  (Accept: text/event-stream → token stream)
     D12 semantic response cache (opt-in per agent, per-tenant partition)
     D13 POST /api/v1/runs/{id}/replay[?against=published|version:X]
     D14 POST /api/v1/runs/{id}/feedback
```

---

# Part 1 — Cross-cutting quality bar (codified as CONTRACT)

Every decision D1–D8 is a quality **every endpoint must meet going forward**.
All are **additive** (R1): no existing field, header, or body is removed or
renamed. New endpoints satisfy these on day one; existing endpoints are
brought into compliance opportunistically as they are next touched (no
big-bang rewrite, no boundary violation).

### D1 — Economics + rate-limit headers on every response

Every response carries, **best-effort** (R2), a small set of economics
headers alongside the existing rate-limit headers:

| Header | Meaning |
|---|---|
| `X-MDK-Cost-USD` | Provider cost attributable to this request (BYOK spend), to 6 dp |
| `X-MDK-Tokens-In` | Prompt tokens consumed |
| `X-MDK-Tokens-Out` | Completion tokens produced |
| `X-MDK-Cache` | `hit` \| `miss` \| `none` (and `semantic` once D12 is enabled) |
| `X-RateLimit-Limit` / `-Remaining` / `-Reset` | Per-key ceiling (**existing** — unchanged) |
| `X-RateLimit-Tenant-Limit` / `-Remaining` / `-Reset` | Per-tenant aggregate (**existing** — unchanged) |
| `Retry-After` | On 429 only (**existing** — unchanged) |

These are **additive and never change response bodies** — a client that
ignores headers behaves exactly as today. They follow the existing
`Response.headers`-stamping pattern (see the implementation note above), set
at the executor seam where cost is known. **Best-effort (R2):** a cost we
cannot yet compute (e.g. a provider whose token pricing isn't wired) emits
the header **omitted entirely**, never a zero or a guess — a missing header
means "unknown," a present header means "authoritative." On streaming
surfaces the end-of-stream cost is delivered as a trailing SSE `usage` frame
(see D11 + R5).

### D2 — Self-teaching error envelope

Extend the existing envelope with **optional** fields. The current shape —

```json
{ "error": { "code": "not_found", "message": "agent 'x' not found" } }
```

— gains (all optional, all additive):

```json
{ "error": {
    "code": "not_found",
    "message": "agent 'x' not found",
    "request_id": "req_01HZ...",        // promoted from docstring-planned to a real optional field
    "docs_url": "https://docs.movate.dev/errors/not_found",
    "fix_hint": "list available agents at GET /api/v1/agents",
    "retriable": false,
    "retry_after_ms": null
} }
```

**The existing `code` and `message` are NEVER removed or renamed** — they
remain required contract. The new fields are added as **explicit `Optional`
fields** on `ErrorBody` (the `extra="forbid"` config means they cannot be
loose extras — see the implementation note above). `retriable` + `retry_after_ms`
let a client decide programmatically whether (and when) to retry without
parsing prose; `docs_url` + `fix_hint` make the API teach its own correct
use. Auth failures (`AUTH_REQUIRED`) still return the **single
non-discriminating shape** (timing-oracle defense, per `errors.py`) — they
MAY carry `docs_url` but MUST NOT carry a `fix_hint` that leaks *why* auth
failed.

### D3 — Universal dry-run

Any mutating endpoint honors a request header `X-Dry-Run: true`, returning
the **would-be effect** (the diff it would apply and/or the cost it would
incur) **without applying it**. Response is `200` with an
`X-MDK-Dry-Run: true` echo header and a body describing the simulated effect;
no state changes, no provider spend (or a *predicted* spend surfaced via the
D1 economics headers marked as an estimate). This **supersedes** the
scattered per-endpoint `?dry_run=` style: where a `?dry_run=` query param
already exists it **remains as a back-compat alias** (deprecated, never
removed without a Sunset cycle per D7). The header is the one universal
control going forward.

### D4 — `wait=` long-poll on async job GETs

Every async-job GET accepts a duration `wait=`:

```
GET /api/v1/jobs/{id}?wait=30s
```

It **blocks until the job reaches a terminal state or the timeout elapses**,
then returns the current state (`200` with the job view either way; the
client inspects `status`). This pairs with the SSE stream (ADR 035 D3 /
`/jobs/{id}/stream`) as the **two access styles** for the same job: long-poll
for simple clients, SSE for live progress.

> **Distinct from the existing `?wait=true/false` on runs.** `runtime/app.py`
> already has a *boolean* `?wait=` on `POST /agents/{name}/runs` that toggles
> **inline execution vs. queue-a-job**. D4's `wait=<duration>` is a different
> thing: a **long-poll on the job-status GET**, not an execution-mode toggle.
> The duration form is parsed only on `GET .../jobs/{id}`; the boolean
> execution-mode form on the run POST is untouched (R1). The two never
> conflate — documented explicitly so the OpenAPI spec and SDKs don't merge
> them.

### D5 — Sparse fieldsets + expand

Read endpoints accept two projection controls:

- `?fields=` — **projection**: return only the named top-level fields
  (e.g. `?fields=id,status,cost_usd`). Reduces payload for list-heavy UIs.
- `?expand=` — **inline related resources**: replace a reference with the
  referenced object inline (e.g. `?expand=agent,project` on a run view).

Both are bounded by a **per-resource allow-list** (no arbitrary field paths,
no unbounded `expand` graph — prevents accidental N+1 fan-out and information
disclosure). Absent the params, responses are **byte-identical to today**
(R1). Unknown field names are ignored (forward-compat for clients on newer
SDKs hitting older runtimes).

### D6 — `_links` (HATEOAS-lite)

Read responses MAY carry an optional `_links` object advertising the **next
valid actions** as `{rel: url}`:

```json
{ "id": "run_01HZ...", "status": "succeeded",
  "_links": {
    "self":     "/api/v1/runs/run_01HZ...",
    "replay":   "/api/v1/runs/run_01HZ.../replay",
    "feedback": "/api/v1/runs/run_01HZ.../feedback",
    "trace":    "/api/v1/runs/run_01HZ.../trace"
} }
```

Optional and additive: a client that ignores `_links` is unaffected (R1).
The set is **scope-aware** — links to actions the caller lacks scope for are
omitted, so `_links` doubles as a lightweight capability hint at the
resource level (complementing D9 at the runtime level). This is the
deliberately-cheap 80% of what GraphQL would give us; see *Alternatives*.

### D7 — Machine-readable changelog + deprecation signaling

- `GET /api/v1/changelog` returns a **per-version, machine-readable**
  changelog: for each CalVer build, the routes added/changed/deprecated, the
  enabled-extra deltas, and the spec hash. This is the **fleet-resilience
  companion to D9**: D9 says *what this runtime can do now*; D7 says *how it
  got here and what's going away*.
- Aging routes carry standard `Deprecation:` and `Sunset:` response headers
  (RFC 8594) with a `Link: rel="deprecation"` to the migration doc. Nothing
  is ever removed without first emitting these headers for at least one
  CalVer release cycle (deprecate-before-remove, CLAUDE.md rule 5).

### D8 — Auto-generated typed SDKs (spec stays codegen-clean)

The OpenAPI spec is the source of truth for **published Python + TS clients,
generated per release**. This ADR commits to the **contract** that the spec
**stays codegen-clean**: every route has a stable `operationId`, every
request/response a named schema, no anonymous inline unions that defeat
codegen, no `additionalProperties: true` where a typed shape is feasible.
The **SDK build pipeline itself is a follow-up implementation** (out of
scope here — see *Boundaries*); this ADR's job is to make the spec worthy of
it and to add a CI contract test that fails the build if the spec regresses
codegen-cleanliness.

---

# Part 2 — Five flagship next-gen capabilities

D9–D14 **reserve the URL space + semantics** so the flagships land coherently.
Each reuses an existing seam rather than inventing a parallel one.

### D9 — Capability Discovery

```
GET /api/v1/capabilities
```

Returns a machine-readable description of **what this specific runtime can
do** — the direct answer to "how does a front end adapt to N heterogeneous
customer runtimes":

```json
{
  "version": "2026.5.28.0",
  "live_adrs": [13, 32, 35, 36, 40, 45],
  "enabled_extras": ["postgres", "sse", "semantic_cache"],
  "available_models": ["claude-…", "gpt-…"],
  "limits": { "max_sse_streams_per_tenant": 50, "max_session_turns": 200,
              "rate_limit_per_min": 600 },
  "feature_flags": { "sessions": true, "semantic_cache": true,
                     "run_replay": true, "feedback": true }
}
```

- **Read-only.** Two views: an **unauthenticated-safe subset** (version,
  coarse feature flags — enough for a login screen to adapt) and the
  **authenticated full** view (models, limits, tenant-scoped flags).
- The unauthenticated subset is deliberately small and carries **no
  tenant-identifying data** — it is the same for every caller hitting that
  runtime.
- The front end caches per-tenant-runtime and re-fetches on a `Deprecation:`
  signal (D7) or version change. This is the keystone for the per-tenant
  version-drift problem.

### D10 — Stateful Sessions

```
POST /api/v1/sessions                    → { session_id }
POST /api/v1/sessions/{id}/messages      → append a turn, get the reply
GET  /api/v1/sessions/{id}               → full session (history + rollups)
```

Server-managed multi-turn memory: history, summarization, truncation, and a
**per-session cost rollup** (sums the D1 economics across turns). New
`sessions` + `session_messages` storage tables, **`tenant_id NOT NULL`** on
both (per-tenant isolation, R6-adjacent). Behind the `StorageProvider`
Protocol — SQLite and Postgres both, never a hardcoded backend (CLAUDE.md
rule 6/7); schema change is flagged per rule 5 and lands in its own migration
PR.

**The executor stays stateless.** Sessions are a **layer above** the
executor: the session service assembles the prompt (history + summary), calls
the unchanged stateless executor, and persists the turn. This preserves the
control-plane ⊥ execution-plane boundary — the executor never learns sessions
exist. By default memory is **server-managed**; a client may pass
`memory: "client"` to opt out and get exactly today's stateless behavior (R3).

### D11 — Run-output token streaming

```
POST /api/v1/agents/{name}/runs
Accept: text/event-stream     → streams OUTPUT tokens as generated
Accept: application/json      → buffered JSON (today's behavior, default)
```

Streams the agent's **output tokens** as they are generated. This is
**distinct from ADR 035 D3's event stream**: D11 is the *output* stream (the
model's tokens); ADR 035 D3 is the *event* stream (lifecycle/webhook-style
events). They are **separate surfaces with separate media-type negotiation
and never conflated** (R5). The transport reuses the existing
`_sse_run_stream` machinery (which already yields `token` frames), so D11 is
largely **codification + media-type negotiation + a trailing `usage` frame**
carrying the D1 economics that aren't known until the stream completes.
Falls back to buffered JSON when `Accept: application/json` — the default, so
existing clients are unaffected (R1). Output streams count against the same
**per-tenant SSE concurrency cap** as the event stream (ADR 035 D3 / D9
`max_sse_streams_per_tenant`) — one budget, not two.

### D12 — Semantic Response Cache

**Opt-in per agent.** When enabled, on a run the runtime embeds the input,
and if a cached response exists within a **TTL window** whose input embedding
has **cosine-similarity > threshold**, it returns the cached response
(surfaced as `X-MDK-Cache: semantic`). A per-request `?semantic_cache=off`
override forces a fresh run.

- **OFF by default**, opt-in per agent (R4). Extends the **existing
  CacheProvider** seam — not a new subsystem.
- **Privacy is structural:** the cache is **per-tenant-partitioned**; input
  embeddings and cached responses are **tenant-scoped and never read across
  tenant boundaries** (R4, R6). A cache lookup for tenant A can only ever hit
  tenant A's partition. This is enforced at the partition key, not by
  convention.
- Watch item: semantic cache trades exactness for recall — a too-low
  threshold returns a *near*-match as if exact. The threshold + TTL are
  per-agent config with conservative defaults, and `X-MDK-Cache: semantic`
  always tells the caller a semantic (not exact) hit occurred so it can
  re-run if it must.

### D13 — Run Replay / Time-Travel

```
POST /api/v1/runs/{id}/replay[?against=published|version:X]
```

Re-executes a **historical run's recorded input** against a chosen agent
version (`published`, or a pinned `version:X`), and returns a **side-by-side**
of the original vs. the replayed output. Reuses the **eval + harvest
infrastructure (ADR 016)** — a replay is a single-case eval whose dataset is
one harvested run — rather than a parallel execution path. The original run
is immutable (it's a historical record); replay never mutates it. Useful for
"would the new prompt have answered this better?" without waiting for live
traffic.

### D14 — Feedback ingestion

```
POST /api/v1/runs/{id}/feedback   { "rating": …, "comment": …, "labels": [...] }
```

Captures end-user signal on a run and **feeds the harvest → eval flywheel**
(ADR 016 D1): rated runs become candidate eval cases, and low-rated runs can
**seed proposed patches** in the self-improving loop (ADR 043). This closes
the **end-user-signal → improvement loop** — the last missing edge: real
usage produces evals and improvement candidates, not just dashboards.
**Writing** feedback is **write-scope**; **reading aggregated** feedback is
**read-scope**; **raw** feedback (free-text comments, which may contain PII)
**never leaves the tenant** (R6).

---

## Resolved decisions (locked in upfront)

- **R1 — Everything in Part 1 is ADDITIVE.** No existing field, header, or
  body is removed or renamed. Back-compat is **absolute** (CLAUDE.md rule 5).
  D2's new error fields are explicit `Optional` fields (not relaxed
  `extra="allow"`); D3/D4/D5/D6 controls are inert when absent; D1 headers
  never touch bodies.
- **R2 — Economics headers are best-effort.** A cost we cannot compute yet
  emits the **header omitted entirely** — never a zero, never a guess. Missing
  = "unknown"; present = "authoritative."
- **R3 — Sessions memory is server-managed by default, client-opt-out
  preserved.** A client may pass `memory: "client"` to get today's exact
  stateless behavior. The executor is stateless regardless; sessions are a
  layer above it.
- **R4 — Semantic cache is OFF by default, opt-in per agent,
  per-tenant-partitioned.** Input embeddings and cached responses are
  tenant-scoped; **no cross-tenant leakage** is structurally enforced at the
  partition key. Threshold + TTL are conservative per-agent config.
- **R5 — Output streaming (D11) and event streaming (ADR 035 D3) are distinct
  surfaces with distinct media-type negotiation; never conflated.** Both share
  the one per-tenant SSE concurrency cap.
- **R6 — Feedback is write-scope; aggregated-feedback read is read-scope; raw
  feedback never leaves the tenant.** Same per-tenant isolation discipline as
  sessions (D10) and semantic cache (D12).

---

## First implementations (in-flight PRs landing tonight)

The following parallel PRs landing tonight are the **first concrete
implementations** of Part 1's D1–D4 and Part 2's D9 — this ADR codifies the
contract they begin to satisfy, and they in turn de-risk the contract by
proving it against the live surface:

- **Capability Discovery** → first implementation of **D9**
  (`GET /api/v1/capabilities`, the unauthenticated-safe subset + authenticated
  full view).
- **Response-envelope ergonomics** → first implementation of **D2** (promoting
  `request_id` to a real optional field + adding `docs_url` / `fix_hint` /
  `retriable` / `retry_after_ms` as explicit `Optional` fields on `ErrorBody`,
  preserving `extra="forbid"`).
- **`wait=` long-poll** → first implementation of **D4** (the duration
  `wait=<n>s` long-poll on the job-status GET, distinct from the existing
  boolean run-mode `?wait=`).
- **Cost prediction** → first implementation of **D1** (the
  `X-MDK-Cost-USD` / `X-MDK-Tokens-*` economics headers, best-effort per R2,
  plus the D3 dry-run cost estimate).

Subsequent flagships (D10–D14) and the remaining quality bars (D5–D8) are
sequenced in follow-up PRs, each one responsibility, each additive.

---

## Consequences

**Positive.**
- **Front ends become version-resilient.** D9 + D7 let one Mova iO adapt to N
  heterogeneous customer runtimes by *asking* each one what it can do, instead
  of degrading to the fleet's lowest common denominator or shipping per-tenant
  special-casing.
- **Front ends become economically transparent.** D1 puts per-call BYOK cost
  on every response; D10's per-session rollup and D14's feedback close the
  loop from spend to quality. Customers can own their cost story.
- **The API becomes conversational + stateful.** D10 (sessions), D11 (output
  streaming), D13 (replay), and D14 (feedback) move the product from
  one-shot-stateless to a genuine multi-turn, observable, improvable surface —
  without making the executor stateful.
- **The API teaches its own use.** D2 (self-teaching errors), D6 (`_links`),
  and D8 (typed SDKs) reduce the integration cost from "needs us in the room"
  to "a customer team can own it."

**Risks / watch items.**
- **Semantic-cache correctness (D12).** A too-low similarity threshold returns
  a near-match as if exact — the headline correctness risk. Mitigation:
  OFF-by-default, conservative per-agent thresholds, `X-MDK-Cache: semantic`
  always disclosed so a caller can re-run, per-tenant partition so a wrong hit
  is at worst within-tenant. Document threshold tuning in the runbook.
- **Session storage growth (D10).** `session_messages` grows unbounded without
  a retention policy; long sessions inflate prompt-assembly cost. Mitigation:
  summarization + truncation are first-class in the session service, and a
  per-tenant retention/TTL is configured alongside the schema (its own
  migration PR).
- **Streaming connection limits (D11).** Output streams add long-lived
  connections. Mitigation: they **reuse the existing per-tenant SSE cap from
  ADR 035 D3** (one budget shared with the event stream, surfaced in D9's
  `limits`) — they do not get a second, separate allowance.
- **Economics-header accuracy (D1).** Per R2 a missing header means "unknown,"
  but consumers must be told not to treat absence as zero. Documented in the
  SDK + the D1 header contract.
- **Spec codegen-cleanliness drift (D8).** Without a guard the spec rots out of
  codegen-cleanliness as routes are added. Mitigation: a CI contract test
  (this ADR's commitment) fails the build on regression.

---

## Alternatives considered

Per-feature, so each rejection is traceable:

- **D10 — Stateless-only (status quo).** *Rejected* for customer experience:
  every client reimplements multi-turn memory, inconsistently, and nobody gets
  a per-session cost rollup. Kept as the **opt-out** (`memory: "client"`, R3),
  not the default.
- **D10 — Client-managed memory only.** *Rejected as the default* — it pushes
  history assembly, summarization, and truncation onto every client and makes
  cross-turn cost rollups impossible server-side. **Kept as the opt-out.**
- **D12 — Exact-cache-only.** *Rejected* — exact-match caching misses
  intent-equivalent duplicates ("what's your return policy?" vs. "how do I
  return something?"), which is the majority of real cache value on a
  conversational surface. Semantic cache captures intent dups; exact cache
  remains the `hit`/`miss` path when semantic is off.
- **D5/D6 — GraphQL instead of sparse-fields + expand.** *Deferred* — `_links`
  (D6) + `expand`/`fields` (D5) cover ~80% of GraphQL's projection/relation
  value with **far less surface area**, no new query language, no new auth
  model, and a spec that stays codegen-clean for D8. A GraphQL gateway remains
  a future option (see *Boundaries*) but is not justified by current need
  (CLAUDE.md rule 8 — no new framework without a proven scaling need).
- **D1 — Economics in the body instead of headers.** *Rejected* — putting cost
  in the body would change every response shape (violating R1) and couple
  every consumer's parser to it. Headers are additive and ignorable.
- **D9 — Static per-version capability manifest shipped with the front end.**
  *Rejected* — a baked-in manifest can't track per-tenant enabled-extras drift
  and goes stale the moment a tenant upgrades. The runtime must answer for
  itself at request time.

---

## Boundaries (out of scope)

- **GraphQL gateway** — a future option if projection/relation needs outgrow
  D5 + D6; not part of this ADR.
- **Multimodal I/O** — a future ADR; it touches the `BaseLLMProvider` provider
  seam (CLAUDE.md rule 7) and deserves its own decision.
- **The SDK build/publish pipeline itself (D8)** — a follow-up implementation.
  This ADR only commits to keeping the OpenAPI spec codegen-clean and adding
  the CI contract test; building and publishing the Python/TS clients is
  separate work.
- **Webhook replay** — extends ADR 035 (events/webhooks) and lands in its own
  PR; D13's *run* replay is distinct (it replays a model run, not a webhook
  delivery) and is not the webhook-replay feature.
- **Implementation of the storage migrations** for D10's `sessions` /
  `session_messages` tables and D12's per-tenant cache partition — flagged here
  (CLAUDE.md rule 5: storage-schema change) and authored in their own
  migration PRs behind the `StorageProvider` Protocol.
