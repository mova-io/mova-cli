# Expense Approval pattern — governance

Topology: `DECISION(amount) → {director-approval | manager-approval | auto} → [HITL routes approve/reject] → shared post-erp → finalize | rejected`

A tiered approval workflow, durable on Temporal — **zero LLM classifiers on the
approval path**. The **amount tier** routes on a deterministic `decision` node
(ADR 094) — no LLM, no Temporal activity — then each tier pauses durably at a
HUMAN approval gate that routes directly on the approver's structured
`decision` (ADR 099): `approve`→ERP-post, `reject`→rejected, any other wording
fails safe to rejected. All tiers **converge on one shared tail** (exclusive
convergence, ADR 098): one `post-erp`, one `finalize`, one `rejected` —
regardless of tier count, so a fix or eval on the shared step lands once
instead of per tier.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic value routing | the `classify` `decision` node — `amount > 5000` → director, `> 100` → manager, else auto. No model call. |
| Durable human-in-the-loop | `manager-approval` / `director-approval` HUMAN nodes pause durably (survive worker restarts) until a `POST /api/v1/workflow-runs/{id}/signal`. |
| Deterministic decision routing on the gate | `routes`/`fallback` on the HUMAN nodes (ADR 099) — trim+casefold exact match on the approver's `decision`; approve→`post-erp`, reject→`rejected`, anything else fails safe to `rejected`. No model call. |
| Self-contained agents | every agent is bundled under `agents/` with correct schemas + JSON-instructed prompts (real LLM output, not stubs). |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Tiering is deterministic | the `decision` node is pure arithmetic over `state.amount` | the routing decision replays identically on Temporal — no model in the control path. |
| Approval routing is deterministic | the HUMAN gates' `routes`/`fallback` (ADR 099) are an exact-match table over the approver's answer | approve/reject replays identically and an out-of-vocabulary answer can only land on the fail-safe `rejected` path. |

## Customize

- Edit the thresholds in the `classify` node's `cases` (e.g. company approval limits).
- Swap the approval chain: add/remove HUMAN tiers — each gate carries its own `routes`/`fallback`, no classifier node to clone.
- Point the strict path elsewhere: `fallback` is the author's answer to "what if the response isn't in the vocabulary" — aim it at a re-prompt/escalation node instead of `rejected` if you want a second ask.
- Point `erp-poster` at your real finance system (today it returns a synthetic posting ref).

## Budget

Per-run LLM spend is bounded: the workflow makes at most 2 model calls on any
path (ERP-post + finalize on approval; just rejected on rejection — the amount
tier AND the approve/reject routing are deterministic, zero-cost). Cap absolute
spend with the agent `budget.max_cost_usd_per_run` field or a governance COST
gate (ADR 093); the eval-gate below is the quality budget.
