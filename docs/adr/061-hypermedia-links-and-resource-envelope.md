# ADR 061 — Hypermedia `_links` + a uniform created-resource envelope

**Status:** Accepted
**Date:** 2026-05-31
**Deciders:** Engineering (control-plane / API)
**Context window:** make the `/api/v1` surface *self-navigating* and *uniform*
for API-first clients (front ends, integrators, a live Postman tour) — the
last leg of the CLI↔API parity / discoverability work (ADR 060, the
`capabilities.resources` block).
**Builds on / composes with (changes nothing in any of them):**
the existing `_links` precedent on the graph API (`NodeDetailView._links`,
`Field(alias="_links")`, already shipped), ADR 040 (projects — whose
create response already carries `project_id` / `created_at` / `updated_at` /
`etag`, the envelope this ADR generalizes), ADR 014 (agent registry), and the
`capabilities.resources` discoverability block (ADR 060 sibling) — `_links`
is the *per-response* answer to the same "what can I do next?" question
`capabilities` answers *globally*.

**Defining gap.** A live audit of the core flow (project → agent → KB → skill
→ run) shows two inconsistencies an API-first client trips on:

1. **No hypermedia.** A response tells you *what happened* but not *what's
   next*. After `POST /projects` you must already know the agent-create URL;
   after `POST …/agents` you must hand-build the validate / KB / publish / run
   paths. The graph API already solved this with `_links` (`expand` →
   neighbors) — but nothing else adopted it.
2. **Non-uniform create envelopes.** `POST /projects` returns
   `project_id` + `created_at` + `updated_at` + `etag`; `POST …/agents`
   returns `agent_name` + `agent_dir` + `files_persisted` + `changed` +
   `attached` — no timestamp, no `etag`, a different id key. A client can't
   treat "a created resource" uniformly.

This is a **design** ADR for a cross-cutting response convention (rule 2). It
is **additive** — every change is a new optional field; no existing field is
renamed, retyped, or removed (rule 5).

---

## Decision

### D1 — `_links` is the standard hypermedia affordance

Every **resource response on the core flow** carries a `_links` object: a
`{rel: url}` map of the sensible next calls, where `rel` is a stable verb
(`self`, `validate`, `kb`, `publish`, `run`, `agents`, …) and `url` is an
absolute-from-root `/api/v1/...` path. Mechanism is the **existing graph
precedent verbatim**:

```python
model_config = ConfigDict(extra="forbid", populate_by_name=True)
links: dict[str, str] = Field(default_factory=dict, alias="_links")
```

`populate_by_name=True` lets the handler construct with `links={...}` while the
wire shape is `_links`. Default `{}` → absent-safe: a client that ignores
`_links` is unaffected, and a response with no obvious next step simply omits
it (empty object).

### D2 — A uniform created-resource envelope

Every **create** response exposes the same identity/concurrency triple the
projects resource already has:

| field | meaning |
|---|---|
| `id` | the resource's canonical id (additive alias for the existing typed key — `project_id`, `agent_name`, … stay) |
| `created_at` | UTC create timestamp |
| `etag` | optimistic-concurrency tag (the `updated_at` the resource already tracks) |

These are **added** alongside the existing fields, never replacing them — an
older client keying off `agent_name` keeps working; a new client can treat
"any created resource" uniformly.

### D3 — Scope: core flow now, the rest incrementally

This ADR establishes the convention and applies it to the **core-flow
responses** — project-create, agent-create, KB-ingest, skill-create, run —
the surface the Postman demo and the front end's create wizard exercise.
Remaining endpoints (members, canary, evals, …) adopt `_links`/the envelope
incrementally as they're touched; the convention, not a big-bang sweep, is the
deliverable. The `capabilities.resources` block (ADR 060) already tells a
client *which* resources exist; `_links` tells it *where to go next* from a
given response — the two compose.

### D4 — Derivation, not hardcoding

`_links` targets are built from the **known route templates** filled with the
response's own ids (e.g. agent-create → `validate: /api/v1/agents/{name}/validate`
with `{name}` substituted). They are not gated on per-request route
introspection — the core-flow routes are always registered on a runtime build —
but they MUST stay in sync with the real paths; the Postman anti-drift test
(`tests/test_postman_collection.py`) and the route table are the backstop.

### D5 — Backward compatibility (additive, rule 5)

`_links`, `id`, `created_at`, `etag` are **new optional fields** with safe
defaults (`{}` / derived). No field is renamed/removed/retyped; no scope,
route, status code, or error shape changes. Clients that predate the fields
ignore them; the `extra="forbid"` model guard is unaffected (these are declared
fields, not extras). No storage-schema change — `created_at`/`etag` are read
from values the resources already persist.

## Consequences

**Positive**
- The core flow becomes **self-navigating** — a Postman/front-end client
  follows `_links` instead of hand-building every next URL.
- A **uniform "created resource"** shape across projects/agents/skills.
- Consistent with the already-shipped graph `_links` — one convention, not two.

**Negative / risks**
- `_links` must not drift from the real routes — mitigated by deriving from the
  same templates the handlers register and the Postman anti-drift test.
- Partial adoption (core flow first) means the surface is briefly non-uniform
  until the long tail adopts it — acceptable; the convention is the contract.

## Boundaries

Pure response-shaping at the API edge (rule 6) — no change to `core`,
`storage`, execution, or scopes. Additive, opt-in to read, bundle/CLI paths
untouched. Mirrors the existing graph `_links` mechanism rather than inventing
one.

## Alternatives considered

- **Full RFC-8288 `Link` headers / HAL+JSON media type.** Rejected — heavier
  than the in-body `_links` the graph API already established; no client asked
  for content-negotiated hypermedia.
- **A big-bang sweep of every response.** Rejected — high churn, low marginal
  value on rarely-chained endpoints; the convention + core-flow application is
  the right first step (D3).
- **Leave envelopes as-is.** Rejected — the non-uniform create shape is exactly
  the kind of seam inconsistency (rule 6) the parity work is meant to remove.

## Scope / rollout

Single PR: this ADR + the core-flow application (`_links` on project/agent/
KB/skill/run create responses + the `id`/`created_at`/`etag` envelope on
agent-create). Long-tail endpoints adopt incrementally (D3).
