# Approval Timeout pattern — governance

Topology: `[HITL primary, 90s durable deadline] → on_timeout → [HITL escalation, 90s durable deadline] → shared TOOL fulfill → notify | rejected (reject routes + final expiry)`

Approval with durable timeout + escalation, durable on Temporal — the live
shape of **ADR 062 D4 durable timeouts**, with **zero LLM calls on the
control path and zero LLM calls on the fulfilment step**. The request pauses
at the primary HUMAN gate, which routes its own structured `decision`
(ADR 099): approve→fulfill, reject→rejected, prose fails safe to rejected.
If the primary approver does not respond within the gate's `timeout` (90s),
the DURABLE timer fires and the run takes `on_timeout` into the escalation
HUMAN gate (the alternate approver) — same vocabulary, its own 90s deadline,
and ITS expiry fails safe to `rejected`: nobody decided ⇒ nothing fulfilled.
Fulfilment is a **TOOL node (ADR 097)**: the workflow-local `sim-fulfill`
python skill records the `{system: fulfillment, action: fulfill}` row
deterministically — no wrapper agent. Both approve paths **converge on one
shared tail** (exclusive convergence, ADR 098 — the timeout legs are
compiler-injected synthetic `human-timeout` edges): one fulfill, one notify,
one rejected.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Durable timeouts (ADR 062 D4) | `timeout: 90` + `on_timeout:` on each HUMAN gate — the deadline is a Temporal timer, so it survives worker restarts and fires with no poller, no cron, no signal. Native ignores it (waits forever) — this pattern is `runtime: temporal` for a reason. |
| Timeout escalation chains | the primary gate's `on_timeout` aims at a SECOND human gate — silence escalates to the alternate approver instead of failing. |
| Fail-safe expiry | the escalation gate's `on_timeout` aims at `rejected` — total silence can only land on the no-side-effect path. Timeout wins over routes (ADR 099 D4): `routes` apply only to a delivered decision. |
| Durable human-in-the-loop | both HUMAN gates pause durably until a `POST /api/v1/workflow-runs/{id}/signal` — or their timer. |
| Deterministic decision routing on the gates | `routes`/`fallback` on each HUMAN node (ADR 099) — trim+casefold exact match; anything else fails safe to `rejected`. No model call. |
| Deterministic skill execution as a step | the `fulfill` TOOL node (ADR 097) — one `dispatch_skill` call, schema-validated in and out, SKILL-gate governed, no LLM. |
| Self-contained workflow-local skill | `skills/sim-fulfill/` carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow. |
| Timeout-aware agents | the `rejected` agent's prompt branches on whether a `decision` was ever delivered (Jinja `is defined`) — the timeout path carries NO decision key, and the prompt says so instead of rendering an undefined. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| A run can never park forever | every HUMAN gate carries a `timeout` + `on_timeout`, and the LAST link's expiry is a terminal route | the workflow's total wait is bounded (here ≤ ~180s) by construction — the ADR 062 "workflow parks indefinitely" risk is closed per-gate, not by an external reaper. |
| Expiry fails safe | the final `on_timeout` lands on `rejected`, never on the TOOL node | silence cannot fulfil anything; the absence of the ledger row is assertable. |
| Approval routing is deterministic | each gate's `routes`/`fallback` (ADR 099) | an out-of-vocabulary answer can only land on the fail-safe `rejected` path. |
| Fulfilment is governed | the TOOL node's skill declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) | the external write cannot hide behind a prompt. |

## Customize

- Tune the deadlines: `timeout:` is seconds — set the primary gate to your
  SLA (4h = 14400) and the escalation gate to the on-call rotation's.
- Lengthen the escalation ladder: each new link is one more HUMAN gate whose
  `on_timeout` aims at the next; keep the LAST link's expiry on `rejected`.
- Point `sim-fulfill` at your real fulfilment backend (ITSM, IAM, badge
  system): swap the impl.py body, keep the schema contract.
- Want expiry to auto-approve instead? Aim the last `on_timeout` at the TOOL
  node — and write the ADR for why silence should mutate an external system.

## Budget

Per-run LLM spend is bounded: **exactly 1 model call on every path** (notify
on the fulfilled paths, rejected on the reject/expiry paths — the gate
routings, the durable timeout legs, and the fulfill TOOL node are
deterministic, zero-cost). Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093); the
eval-gate below is the quality budget.
