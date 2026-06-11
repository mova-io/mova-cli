# Multi-Agent Business Process pattern — governance

Topology: `SUPERVISOR(process-manager ⇒ research | pricing | compliance, ≤4 rounds) → proposal → notify`

A business process under the bounded SUPERVISOR primitive (ADR 092 D4),
durable on Temporal. The `process-manager` agent delegates across a FIXED
specialist allowlist — `research`, `pricing`, `compliance`, each a calibrated
sim producing its own JSON findings key — until it answers the sentinel
`done` or the hard `max_delegations` cap trips. A `proposal` agent then
composes the deliverable from the three findings and `notify` confirms it.
This is the managerial pattern WITH the bounds that make it enterprise-grade
— vs. the unbounded recursive spawning of an open swarm (ADR 038 D5,
declined).

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Bounded managerial delegation (ADR 092 D4) | the `supervisor` node: manager + fixed `specialists` allowlist + `max_delegations: 4` — the loop is internal to the node, so the graph stays linear/acyclic. |
| Deterministic Temporal lowering (Phase 3b) | the supervisor compiles to a `for _ in range(4)` loop of manager + allowlisted-specialist activities — bounded, deterministic, replayable; native runs the same loop with identical final state. |
| State-driven management | each round the manager sees the findings gathered so far (`is defined`-guarded in its prompt) and picks the next missing consultation — the delegation order is observable in state, not hidden in a transcript. |
| Specialist JSON contracts | each specialist writes its OWN labeled key (`research_findings` / `pricing_quote` / `compliance_assessment`) — the proposal composer reads the three contracts, never raw chatter. |
| Calibrated specialist sims | pricing quotes ONLY from its embedded rate card; compliance assesses ONLY against its embedded checklist (HIPAA ⇒ Enterprise + BAA, EU residency ⇒ eu-west + DPA) — swap the corpus for your real systems to go live. |
| Self-contained agents | all six bundled under `agents/` with correct schemas + JSON-instructed prompts. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| Delegation cap | `max_delegations: 4` on the supervisor node | a manager that never says `done` still terminates after 4 rounds — the anti-runaway bound, enforced by the runner AND the compiled `range(4)` loop. |
| Fixed allowlist | the `specialists` map in `workflow.yaml` | the manager may delegate ONLY to research/pricing/compliance; an out-of-roster answer ends the loop (it cannot reach beyond the roster). Widening the roster is a reviewable edit to this file, not a runtime decision. |
| Reserved sentinel | `done` cannot be a specialist id (spec-validated) | the terminate signal can never be shadowed by a roster entry. |
| No cycles | the delegation loop is INTERNAL to the one node; the graph compiles acyclic | the supervisor composes with every other primitive without runaway topology. |
| Per-call budget | every manager turn and specialist run goes through the executor's per-run budget (default `max_cost_usd_per_run` = 1.0) + the catalog policy's MODEL + COST gates (ADR 093); an optional supervisor-level `budget:` adds an aggregate cap across the whole loop (ADR 092 D5) | the loop's spend is bounded twice: structurally (rounds) and financially (budgets). |

## Customize

- Swap the sims for real systems: point `pricing` at your CPQ/rate API and
  `compliance` at your GRC checklist — keep the one-labeled-key contract per
  specialist.
- Widen the roster: add an agent dir + one `specialists:` entry (a
  reviewable diff). Raise `max_delegations` accordingly (rounds = roster
  size + 1 for the closing `done`).
- Add an aggregate cost ceiling: set `budget:` on the supervisor node to cap
  the WHOLE delegation loop's spend (ADR 092 D5).
- Gate the deliverable: insert a `human` gate between `proposal` and
  `notify` for sign-off before the proposal ships.

## Budget

Per-run LLM spend is bounded: **at most 9 model calls on every path** (up to
4 manager turns + 3 specialist runs + proposal + notify) — the delegation cap
makes the worst case structural, and each call is capped by the agent's
per-run budget.
