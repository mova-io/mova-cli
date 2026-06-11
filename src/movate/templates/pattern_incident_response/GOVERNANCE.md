# Incident Response pattern — governance

Topology: `diagnose → DECISION(confidence) → {TOOL remediate → verify → DECISION(resolved) → notify | [HUMAN escalate routes ack] → notify}`

Event-driven diagnosis + remediation + escalation, durable on Temporal. The
`diagnose` agent names the root cause and self-scores a **calibrated**
confidence (an explicit rubric: 0.9 when the alert message names the fault,
0.3 when it is vague, never in between); a deterministic `decision` node
(ADR 094) routes `confidence gte 0.7` to automated remediation — **no second
LLM judging the first**. The `sim-remediate` TOOL (ADR 097) records the
ATTEMPT as an auditable `{system: ops, action: remediate}` ledger row whether
or not it works, and returns a deterministic `applied`/`failed` status (a
hardware/physical/manual fault cannot be auto-fixed). The `verify` agent
mirrors that machine status into `resolved` — it may never claim resolution
the ops system did not report — and a second `decision` node routes
unresolved runs to the ONE `escalate` HUMAN gate shared by both escalation
reasons (exclusive convergence, ADR 098). The gate routes its own
acknowledgement (ADR 099); its fallback is also `notify` — a human can delay
closure, never wedge it.

## Trigger binding (ADR 100)

The input shape (`{alert: {service, severity, message}}`) is exactly what a
webhook trigger created with `--event-key alert` feeds the workflow:

```
mdk trigger create incident-response -k workflow --name alertmanager \
    --auth-mode token --dedup-key id --event-key alert
```

The alert source POSTs its JSON body to the printed webhook URL; the body
nests under `alert` in workflow state, and its `id` field dedups redeliveries.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Calibrated-confidence routing | the diagnose rubric + the `confidence-gate` decision node (ADR 094) — deterministic threshold, no LLM in the control path. |
| Auditable remediation attempts | the `remediate` TOOL records the attempt row even when it fails — automation's audit trail is complete. |
| Machine-status-grounded verification | the verify agent's rubric pins `resolved` to the ops system's status — optimism cannot close an incident. |
| One escalation gate, two reasons | low confidence and failed remediation converge on the ONE `escalate` HUMAN gate (ADR 098/099). |
| Fail-open acknowledgement | the gate's fallback is `notify` — an unparseable ack delays closure rather than wedging the durable run. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway diagnose→remediate loop is impossible by construction. |
| Remediation failure is deterministic | `_UNREMEDIABLE_MARKERS` in `skills/sim-remediate/impl.py` | the failed path is case-input-reproducible, not LLM-moody. |
| Uncertain diagnoses cannot act | the `confidence-gate` default is `escalate` | below-threshold confidence means automation never touches the ops system. |
| The remediation is governed | `sim-remediate` declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) | the external write cannot hide behind a prompt. |

## Customize

- Point `sim-remediate` at your real runbook automation: swap the impl.py
  body, keep the schema contract (and the recorded-attempt posture).
- Tighten the confidence threshold by editing the `confidence-gate` case —
  a reviewable one-line change, not a prompt tweak.
- Want the escalation to be able to BLOCK closure? Change the gate's
  `fallback` to a rejected-style agent — that makes silence terminal, so
  pair it with a durable timeout (ADR 062 D4, see the approval-timeout
  pattern).

## Budget

Per-run LLM spend is bounded: **at most 3 model calls per run** (diagnose +
verify + notify; escalated-without-remediation paths make 2 — both decision
nodes and the remediation TOOL are deterministic, zero-cost). Cap absolute
spend with the agent `budget.max_cost_usd_per_run` field or a governance
COST gate (ADR 093).
