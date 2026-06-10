# ITSM Service Request pattern — governance

Topology: `DECISION(auto_approved) → {provision | approval} → [HITL routes approve/reject] → shared TOOL provision → notify | rejected`

ITSM service-request fulfilment over a parameterized business catalog
(password reset, VPN access, email group, software license, hardware,
onboarding), durable on Temporal — **zero LLM calls on the control path and
zero LLM calls on the fulfilment step**. The portal's `auto_approved` flag
routes on a deterministic `decision` node (ADR 094); needs-approval services
pause durably at ONE HUMAN gate that routes directly on the approver's
structured `decision` (ADR 099): `approve`→provision, `reject`→rejected, any
other wording fails safe to rejected. Fulfilment is a **TOOL node (ADR 097)**:
the workflow-local `sim-provision` python skill records the provisioning
deterministically — no wrapper agent, the side-effect is declared in
`workflow.yaml` where `mdk validate` and the SKILL gate can see it. Auto +
approve paths **converge on one shared tail** (exclusive convergence,
ADR 098): one provision, one notify, one rejected — regardless of catalog
size.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic value routing | the `classify` `decision` node — `auto_approved == true` → provision, else → the approval gate. No model call. |
| Durable human-in-the-loop | the `approval` HUMAN node pauses durably (survives worker restarts) until a `POST /api/v1/workflow-runs/{id}/signal`. |
| Deterministic decision routing on the gate | `routes`/`fallback` on the HUMAN node (ADR 099) — trim+casefold exact match on the approver's `decision`; approve→`provision`, reject→`rejected`, anything else fails safe to `rejected`. No model call. |
| Deterministic skill execution as a step | the `provision` TOOL node (ADR 097) — one `dispatch_skill` call, schema-validated in and out, SKILL-gate governed, no LLM. |
| Self-contained workflow-local skill | `skills/sim-provision/` carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow, no external package. |
| Self-contained agents | `notify` / `rejected` bundled under `agents/` with correct schemas + JSON-instructed prompts. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Catalog routing is deterministic | the `decision` node is a pure predicate over `state.auto_approved` | the routing decision replays identically on Temporal — no model in the control path. |
| Approval routing is deterministic | the HUMAN gate's `routes`/`fallback` (ADR 099) are an exact-match table over the approver's answer | approve/reject replays identically and an out-of-vocabulary answer can only land on the fail-safe `rejected` path. |
| Fulfilment is governed | the TOOL node's skill declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) statically and at runtime | the external write cannot hide behind a prompt. |

## Customize

- Extend the catalog: the workflow is parameterized — new services need no new
  nodes, just a portal entry with the right `auto_approved` flag.
- Point `sim-provision` at your real ITSM/identity backend (AD, Okta,
  ServiceNow): swap the impl.py body, keep the schema contract.
- Add approval tiers: clone the `approval` gate with its own `routes` — each
  gate carries its own vocabulary, no classifier node to clone.
- `fallback` is the author's answer to "what if the response isn't in the
  vocabulary" — aim it at a re-prompt/escalation node instead of `rejected`
  if you want a second ask.

## Budget

Per-run LLM spend is bounded: at most **1 model call on the auto path**
(notify — classify and the provision TOOL node are deterministic, zero-cost)
and at most **2 model calls on the approval path** (the gate routing is
deterministic; in this shipped shape the approval path also makes exactly 1
call — notify on approve, rejected on reject — comfortably inside the bound).
Cap absolute spend with the agent `budget.max_cost_usd_per_run` field or a
governance COST gate (ADR 093); the eval-gate below is the quality budget.
