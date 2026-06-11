# Executive Briefing pattern — governance

Topology: `TOOL gather-metrics → TOOL gather-incidents → digest → DECISION(risk_count) → {flag | archive}`

Scheduled multi-source executive digest, durable on Temporal and **cron-born
by design** (ADR 100 D1): the production binding is `mdk schedule set
executive-briefing -k workflow --cron "0 7 * * 1-5" --tz America/New_York
--input '{"period": "daily"}'` — the schedule tick enqueues the exact
JobRecord a manual `POST /run` produces, so the definition carries nothing
schedule-specific. **Zero LLM calls on the source pulls and zero LLM calls on
the control path.** Source gathering is a SEQUENTIAL chain of two **TOOL
nodes (ADR 097)** — entrypoint fan-out is not available for TOOL nodes — each
running a workflow-local python skill that returns canned, replay-identical
data and records one auditable `{system: metrics|incidents, action: gather}`
ledger row. The ONE composing LLM call (`digest`) writes the briefing
STRICTLY from the two gathered results under mechanical risk rules and emits
a `risk_count`; the deterministic `decision` node (ADR 094) routes
`risk_count > 0` to the `flag` escalation writer and a clean digest to the
`archive` filer.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Cron-born workflows | the ADR 100 D1 `mdk schedule set --cron` binding documented above — the clock starts the run through the one job path, with provenance (`origin`). |
| Deterministic skill execution as steps | the `gather-metrics` / `gather-incidents` TOOL nodes (ADR 097) — one `dispatch_skill` call each, schema-validated in and out, SKILL-gate governed, no LLM. |
| Sequential multi-source gathering | the TOOL chain — fan-out is deliberately not used (not available for TOOL nodes); the chain replays identically on Temporal. |
| Grounded composition | the `digest` agent's input schema admits ONLY `period` + the two gathered results — the briefing cannot see (or invent from) anything else. |
| Deterministic value routing | the `risk-gate` `decision` node — `risk_count > 0` → flag, else → archive. No model call. |
| Self-contained workflow-local skills | each skill carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow, no external package. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Sources are deterministic | the gather TOOL nodes return canned data and never do network IO | the gather replays identically on Temporal — and going live swaps the impl body, not the topology. |
| Risk routing is deterministic | the `decision` node is a pure predicate over `state.risk_count` | the escalation decision replays identically — no model in the control path. |
| Composition is grounded | the digest's risk rules are mechanical (success-rate floor, budget, open incidents) and its input is schema-narrowed to the gathered results | a hallucinated risk cannot enter the briefing's routing key honestly, and the eval-gate below scores exactly that. |
| Gathering is governed | both skills declare `side_effects: mutates-state` and clear the SKILL gate (ADR 093/097) statically and at runtime | the source pulls cannot hide behind a prompt. |

## Customize

- Bind your real cadence: change the cron expression / timezone in the
  `mdk schedule set` command — the workflow definition stays untouched
  (a schedule is a deployment/tenant binding, ADR 100).
- Point the gather skills at your real metrics/incident backends: swap each
  impl.py body, keep the schema contracts.
- Add a source: clone a gather skill + TOOL node into the sequential chain
  and widen the digest's input schema — the risk-gate stays unchanged.
- Tune the risk rules in `agents/digest/prompt.md` (they are prose rules the
  LLM applies mechanically) — or harden them into a deterministic skill if
  your thresholds must be provable.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls on every path**
(digest, then flag or archive — both gather TOOL nodes and the risk-gate
routing are deterministic, zero-cost). Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093); the
eval-gate below is the quality budget.
