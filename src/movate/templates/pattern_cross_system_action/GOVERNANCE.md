# Cross-System Action pattern — governance

Topology: `plan → TOOL crm-update → TOOL erp-update → TOOL create-ticket → TOOL send-email → audit-summary`

One business action coordinated across four systems with an audit trail,
durable on Temporal — **zero LLM calls on any system touch**. The `plan`
agent only *restates* the action; the four TOOL nodes (ADR 097) are the
action: CRM (`{system: salesforce, action: update_record}`) → ERP
(`{system: sap, action: update_vendor}`) → ticket
(`{system: servicenow, action: create_ticket}`) → email
(`{system: email, action: send}`), each recording its own auditable ledger
row. The `audit-summary` agent writes the one audit record naming all four
references, in order.

## The order guarantee (the headline of this pattern)

The execution order is **structural**, not behavioral: the graph is a strict
sequential chain and the Temporal lowering executes one activity at a time,
so the ERP can never be updated before the CRM and the notification can
never outrun the ticket. A mid-chain failure leaves a clean ledger PREFIX —
rows in chain order up to the failed step — which is exactly the artifact an
auditor (or a compensating workflow) wants: you can always tell how far the
action got. Reordering the chain is a reviewable edit to the edges in this
file; no prompt can do it.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Ordered multi-system coordination | the four TOOL nodes in a fixed sequential chain (ADR 097) — schema-validated in and out, no model call. |
| Per-system audit rows | each skill records its `{system, action}` ledger row — count + order are assertable per run. |
| One audit record | the `audit-summary` agent runs after all four results merged into state and must cite every reference. |
| Descriptive-only planning | the `plan` agent cannot add, drop, or reorder system touches — the chain is the graph, not the prompt. |
| Self-contained workflow-local skills | every skill carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow, no external package. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| The system set is structural | four TOOL nodes wired in the graph | the model cannot touch a fifth system or skip one — widening the chain is a reviewable edit to this file. |
| Order is structural | the sequential edges + one-activity-at-a-time Temporal lowering | no prompt, retry, or race can reorder the side-effects. |
| Side-effects are governed | all four sim TOOLs declare `side_effects: mutates-state` and clear the SKILL gate (ADR 093/097) | the external writes cannot hide behind a prompt. |

## Customize

- Point the four sim skills at your real Salesforce / SAP / ServiceNow /
  email backends: swap the impl.py bodies, keep the schema contracts.
- Need an approval before the chain runs? Insert a HUMAN gate with `routes`
  (ADR 099) between `plan` and `crm-update` — the whole chain then sits
  behind one veto.
- Need compensation on mid-chain failure? The ledger prefix tells you
  exactly which steps committed; pair it with a compensating workflow per
  system rather than making the chain "smart".

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls per run** (plan +
audit-summary — all four system-touch TOOLs are deterministic, zero-cost).
Cap absolute spend with the agent `budget.max_cost_usd_per_run` field or a
governance COST gate (ADR 093).
