# Purchase Order pattern — governance

Topology: `DECISION(amount) → [HITL manager routes approve/reject] → DECISION(escalate) → [HITL director routes approve/reject] → shared TOOL create-po → notify | rejected`

Tiered purchase-order approval with a SEQUENTIAL APPROVAL CHAIN, durable on
Temporal — **zero LLM calls on the control path and zero LLM calls on the PO
creation step**. A deterministic `decision` node (ADR 094) tiers on the
amount: ≤500 auto-issues the PO; everything else pauses durably at the
manager HUMAN gate, which routes its own structured `decision` (ADR 099):
approve→`escalate-check`, reject→rejected, any other wording fails safe to
rejected. The deterministic `escalate-check` then CHAINS >5000 orders into
the director HUMAN gate — manager AND director must both approve before the
PO exists; one manager gate serves both tiers (no clone). PO creation is a
**TOOL node (ADR 097)**: the workflow-local `sim-create-po` python skill
records the `{system: erp, action: create_po}` row deterministically — no
wrapper agent, the side effect is declared in `workflow.yaml` where `mdk
validate` and the SKILL gate can see it. Auto + both approve paths **converge
on one shared tail** (exclusive convergence, ADR 098): one create-po, one
notify, one rejected.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic value tiering | the `classify` `decision` node — `amount <= 500` → create-po, else → the manager gate. No model call. |
| Sequential approval chains | manager approve routes into `escalate-check`, whose `amount > 5000` case CHAINS into the director gate — two gates in series, both required, the second only reachable through the first's approve. |
| Durable human-in-the-loop | both HUMAN gates pause durably (survive worker restarts) until a `POST /api/v1/workflow-runs/{id}/signal`. |
| Deterministic decision routing on the gates | `routes`/`fallback` on each HUMAN node (ADR 099) — trim+casefold exact match on the approver's `decision`; anything else fails safe to `rejected`. No model call. |
| Deterministic skill execution as a step | the `create-po` TOOL node (ADR 097) — one `dispatch_skill` call, schema-validated in and out, SKILL-gate governed, no LLM. |
| Self-contained workflow-local skill | `skills/sim-create-po/` carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow, no external package. |
| Self-contained agents | `notify` / `rejected` bundled under `agents/` with correct schemas + JSON-instructed prompts. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Tiering is deterministic | both `decision` nodes are pure predicates over `state.amount` | the chain membership replays identically on Temporal — no model in the control path. |
| Approval routing is deterministic | each HUMAN gate's `routes`/`fallback` (ADR 099) are an exact-match table over the approver's answer | an out-of-vocabulary answer can only land on the fail-safe `rejected` path. |
| The chain gates the side effect | `create-po` is reachable from the director gate ONLY via its approve route (and from the manager tier only after the manager's approve) | a director reject after a manager approve provably leaves zero ERP rows. |
| PO creation is governed | the TOOL node's skill declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) statically and at runtime | the external write cannot hide behind a prompt. |

## Customize

- Move the tier boundaries: edit the two `decision` predicates (`lte 500`,
  `gt 5000`) — no new nodes.
- Lengthen the chain: add another `escalate-check`-style decision after the
  director's approve route and a third gate (VP) behind it — each link is one
  decision + one gate.
- Point `sim-create-po` at your real ERP (SAP, NetSuite, Coupa): swap the
  impl.py body, keep the schema contract.
- `fallback` is the author's answer to "what if the response isn't in the
  vocabulary" — aim it at a re-prompt/escalation node instead of `rejected`
  if you want a second ask.

## Budget

Per-run LLM spend is bounded: **exactly 1 model call on every path** (notify
on the created-PO paths, rejected on the reject paths — both decisions, both
gate routings, and the create-po TOOL node are deterministic, zero-cost).
Cap absolute spend with the agent `budget.max_cost_usd_per_run` field or a
governance COST gate (ADR 093); the eval-gate below is the quality budget.
