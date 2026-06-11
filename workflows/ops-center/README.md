# ops-center — AI ops-center daily summary (scenario #27)

Durable Temporal workflow producing the daily ops summary from the unified
observability reporting surface (ADR 096). Topology:

```
TOOL fetch-facts → summarize (LLM) → DECISION(failure_count)
    → {page (HUMAN, ack → report, fallback report) | report}
```

* **Facts in, facts only (ADR 096/097).** The entry `fetch-facts` TOOL node
  runs the workflow-local `sim-fetch-facts` python skill: canned,
  replay-identical rows shaped exactly like `observability_facts` (the flat
  columns `GET /api/v1/observability/facts` serves — `fact_id`, `kind`,
  `source_id`, `status`, `runtime`, `governance_effect`, ...), keyed by the
  `profile` input knob (`steady` | `degraded`). It records one auditable
  `{system: observability, action: fetch_facts}` ledger row per pull.
* **`facts_endpoint` is documentation, not a connection.** The skill accepts
  the real facts endpoint as an optional input — in production that is
  `GET /api/v1/observability/facts` on the runtime API — but the sim NEVER
  does network IO: it returns the canned rows and echoes the endpoint it
  would have queried in `facts_source`. To go live, swap the impl body for a
  real (authenticated) GET against that endpoint and keep the schema
  contract.
* **One summarizing LLM call.** `summarize` writes totals / failures /
  top risks strictly from the rows and emits the `failure_count` integer.
* **Deterministic paging (ADR 094/099).** `failure_count > 0` pauses
  durably at the `page` HUMAN gate; the on-call's `ack` routes to the
  report, and ANY other wording falls back to the report too — fail-open by
  design: a page can delay the daily report, never kill it. A clean window
  skips the page entirely.
* **Path-exclusive state, guarded.** `decision` exists only on the paged
  path, so the report prompt reads it via `| default("n/a")` (the Jinja
  StrictUndefined rule).

## Certification

Mirrors `certification/scenarios/ops-center/` (cases: degraded facts →
page + ack → report; steady facts → report directly, asserting the
`fetch_facts` ledger row and the echoed `facts_source`) and ships as the
`ops-center` pattern template (`mdk init --pattern ops-center`).
