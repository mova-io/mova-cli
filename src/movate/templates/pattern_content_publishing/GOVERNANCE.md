# Content Publishing pattern — governance

Topology: `compliance → DECISION → brand → DECISION → [HUMAN routes approve|reject] → TOOL publish → notify | rejected`

Multi-stage review chain with a HUMAN final gate, durable on Temporal.
**Nothing reaches the CMS without three independent green lights**: a
calibrated `compliance-review` agent (legal/regulatory risk), a calibrated
`brand-review` agent (brand voice), and a human approver. Each review emits
the same enum-pinned `{verdict: pass|flag, notes}` contract, and after EACH
one a deterministic `decision` node (ADR 094) routes on the verdict — `pass`
continues, anything else **fails safe** to the shared `rejected` agent. The
`final-approval` HUMAN gate pauses durably and routes its own structured
decision (ADR 099: approve→publish, reject→rejected, prose fails safe to
rejected). Publication itself is a TOOL node (ADR 097): the workflow-local
`sim-publish` skill records the auditable `{system: cms, action: publish}`
ledger row deterministically — the row that exists exactly once on the
approved path and never on any other.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Separation of review duties | two SEQUENTIAL calibrated reviewers with disjoint rubrics — compliance is told tone is not its job, brand is told legal risk is not its job. One model cannot wave its own work through both checks. |
| Deterministic gating on LLM verdicts | the `compliance-gate` / `brand-gate` `decision` nodes (ADR 094) — the models judge, the route tables are pure, and an out-of-vocabulary verdict can only land on `rejected`. |
| Durable human-in-the-loop with direct routing | the `final-approval` HUMAN node (ADR 099) — pauses durably, trim+casefold exact match on the approver's `decision`, fail-safe fallback. |
| Auditable publication | the `publish` TOOL node (ADR 097) — one `{system: cms, action: publish}` ledger row per approved run, recorded by deterministic code, not by a prompt. |
| Shared rejection tail with context | every flagged/rejected exit converges on ONE `rejected` agent (ADR 098) that relays the latest reviewer notes (or the human veto) back to the author. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway revise-loop is impossible by construction (add one deliberately with a judge node + max_iterations if you want it). |
| The verdict vocabulary is closed | both reviewers' output-schema enum (`pass`/`flag`) | a creative verdict cannot invent a third route; gates fail safe to `rejected`. |
| The human gate cannot be bypassed | `publish` is reachable ONLY via the gate's `approve` route | the graph, not a convention, guarantees the veto. |
| Prose answers fail safe | the gate's `fallback: rejected` (ADR 099) | an out-of-vocabulary approval ("looks fine I guess") rejects rather than publishes. |
| Publication is governed | `sim-publish` declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) | the external write cannot hide behind a prompt. |

## Customize

- Add a review stage: clone an agent + its decision gate into the chain —
  the shared `rejected` tail absorbs the new flag route unchanged.
- Point `sim-publish` at your real CMS: swap the impl.py body, keep the
  schema contract.
- Want flagged content to loop back for revision instead of rejecting?
  Replace the `rejected` agent with a revise agent + a judge node bounded by
  `max_iterations` (ADR 056) — keep the bound.
- `fallback` is the author's answer to "what if the approver's response
  isn't in the vocabulary" — aim it at a re-prompt/escalation node instead
  of `rejected` if you want a second ask.

## Budget

Per-run LLM spend is bounded: **at most 3 model calls per run** (compliance
+ brand + notify/rejected — both decision gates, the HUMAN routing, and the
publish TOOL are deterministic, zero-cost; a stage-1 flag stops after 2).
Cap absolute spend with the agent `budget.max_cost_usd_per_run` field or a
governance COST gate (ADR 093); the eval-gate below is the quality budget.
