# Employee Onboarding pattern — governance

Topology: `plan → TOOL provision-ad → TOOL provision-email → TOOL provision-equipment → welcome`

New-hire provisioning across three systems, durable on Temporal — **zero LLM
calls on any provisioning step**. The `plan` agent only *describes* the plan;
the three TOOL nodes (ADR 097) are the plan: each runs a workflow-local
python skill that records one auditable ledger row — the AD account
(`{system: identity, action: provision_ad}`), the mailbox
(`{system: email, action: provision_mailbox}`), and the equipment order
(`{system: itsm, action: order_equipment}`). The equipment bundle is keyed to
the role through a **fixed map in the skill** — adding a role's hardware is a
reviewable code edit, never a model choice. The `welcome` agent summarizes
what the systems confirmed, with their reference ids.

## Why sequential, not parallel (read before "optimizing")

The three provisioning steps are conceptually a fan-out diamond — and this
template deliberately does NOT ship one. ADR 092's parallel phase gate
(`validate_dag`, Phase 1) admits **agent-only** branches, and the Temporal
lowering (`_emit_fan_out_node`, Phase 2) emits every branch via
`call_agent_activity` — a TOOL node inside a fan-out is rejected at
validation, and would be dispatched as an agent even if it weren't. When a
later ADR 092 phase admits TOOL branches, flip the three sequential edges to
`kind: fan_out`/`fan_in` around the provision nodes: their output keys
(`ad_result`/`email_result`/`equipment_result`) are already disjoint, so the
default `last_wins` join is safe by construction.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic multi-system provisioning | the three TOOL nodes (ADR 097) — schema-validated in and out, no model call, one ledger row each. |
| Role-keyed fulfilment without an LLM | `skills/sim-provision-equipment/impl.py` — a fixed role→bundle map resolves the hardware; the result string names the role + bundle so the choice is assertable from final state. |
| Auditable side-effects | each skill records `{system, action}` to the sim ledger — the rows a suite (or an auditor) can count per run. |
| Descriptive-only planning | the `plan` agent cannot add, drop, or reorder steps — the chain is the graph, not the prompt. |
| Self-contained workflow-local skills | every skill carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow, no external package. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| The provisioning set is structural | three TOOL nodes wired in the graph | the model cannot provision a fourth system or skip one — widening the set is a reviewable edit to this file. |
| Equipment is a fixed map | `_BUNDLES` in the equipment skill | hardware spend is never a model decision; unknown roles get the standard bundle. |
| Side-effects are governed | all three sim TOOLs declare `side_effects: mutates-state` and clear the SKILL gate (ADR 093/097) | the external writes cannot hide behind a prompt. |

## Customize

- Point `sim-provision-ad` / `sim-provision-email` / `sim-provision-equipment`
  at your real identity / email / ITSM backends: swap the impl.py bodies,
  keep the schema contracts.
- Extend the equipment map: add a role to `_BUNDLES` in
  `skills/sim-provision-equipment/impl.py` (a reviewed edit, with a test).
- Need an approval before ordering hardware? Insert a HUMAN gate with
  `routes` (ADR 099) before `provision-equipment`.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls per run** (plan +
welcome — all three provisioning TOOLs are deterministic, zero-cost). Cap
absolute spend with the agent `budget.max_cost_usd_per_run` field or a
governance COST gate (ADR 093).
