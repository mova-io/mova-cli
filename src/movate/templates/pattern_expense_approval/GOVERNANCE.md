# Expense Approval pattern — governance

Topology: `DECISION(amount) → {director-approval | manager-approval | auto} → [HITL] → ROUTER(approve/reject) → shared post-erp → finalize | rejected`

A tiered approval workflow, durable on Temporal. The **amount tier** routes on a
deterministic `decision` node (ADR 094) — no LLM, no Temporal activity — then
each tier pauses durably at a HUMAN approval gate; an LLM classifier reads the
approver's free-text decision and routes approve→ERP-post or reject. All tiers
**converge on one shared tail** (exclusive convergence, ADR 098): one
`post-erp`, one `finalize`, one `rejected` — regardless of tier count, so a fix
or eval on the shared step lands once instead of per tier.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic value routing | the `classify` `decision` node — `amount > 5000` → director, `> 100` → manager, else auto. No model call. |
| Durable human-in-the-loop | `manager-approval` / `director-approval` HUMAN nodes pause durably (survive worker restarts) until a `POST /api/v1/workflow-runs/{id}/signal`. |
| LLM decision classification | `*-decision` intent-routers read the approver's free-text response and route approve/reject. |
| Self-contained agents | every agent is bundled under `agents/` with correct schemas + JSON-instructed prompts (real LLM output, not stubs). |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Tiering is deterministic | the `decision` node is pure arithmetic over `state.amount` | the routing decision replays identically on Temporal — no model in the control path. |

## Customize

- Edit the thresholds in the `classify` node's `cases` (e.g. company approval limits).
- Swap the approval chain: add/remove HUMAN tiers + their `*-decision` routers.
- Point `erp-poster` at your real finance system (today it returns a synthetic posting ref).

## Budget

Per-run LLM spend is bounded: the workflow makes at most 3 model calls on any
path (one `*-decision` classifier + ERP-post + finalize/rejected — the amount
tier itself is deterministic, zero-cost). Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093); the
eval-gate below is the quality budget.
