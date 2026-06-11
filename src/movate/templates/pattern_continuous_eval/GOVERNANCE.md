# Continuous Eval pattern — governance

Topology: `scorer → DECISION(score < 0.6) → {TOOL alert → escalate | TOOL record → ack}`

ONE increment of a scheduled production-quality sampling pipeline, durable
on Temporal. A sampled production interaction (`{sample: {prompt,
response}}`) is scored 0-1 by a CALIBRATED `scorer` agent (its prompt pins
explicit examples on both sides of the floor); the `quality-gate` decision
node (ADR 094) applies the 0.6 floor as a pure numeric predicate. A
regression takes the alarm path — the `sim-alert` TOOL (ADR 097) records
the auditable `{system: eval, action: quality_alert}` row, then the
`escalate` agent writes the summary for the eval owner. A healthy sample
takes the `sim-record-score` TOOL (`record_score` row) into a one-line
`ack`.

**Scheduling (ADR 100):** register the deployed workflow on the runtime
scheduler — `mdk schedule set continuous-eval -k workflow --name
continuous-eval-sampler --cron "*/30 * * * *" --input '{...sample...}'` —
and every cron fire enqueues one increment through the SAME job path a
manual `POST /run` uses (ADR 100 adds zero new start paths). An ADR 100
event trigger can equally deliver each sampled interaction as it happens.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Calibrated LLM scoring | the scorer's rubric + pinned examples either side of the 0.6 floor — the score is an LLM judgment, stated honestly, made reliable by calibration. |
| Deterministic alarm floor | the `quality-gate` decision node (ADR 094) — the scorer judges, the routing stays mechanical. |
| Auditable quality trail | BOTH paths record a ledger row (ADR 097): every sampled increment leaves either a `record_score` or a `quality_alert` row — no silent samples. |
| Schedule-driven increments | the workflow is one increment; cadence lives in the scheduler (ADR 100), not in a loop. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| One sample per run | the workflow shape (no loop, no fan-out) | backpressure and cost stay linear in the schedule cadence. |
| Harm caps the score | the scorer rubric (0.0-0.2 for harmful content regardless of correctness) | a polite, mostly-correct answer that solicits credentials still alarms. |
| The floor is in ONE place | the decision node's `lt 0.6` | tune the alarm threshold without touching the scorer. |

## Customize

- Point `sim-alert` at your real paging system and `sim-record-score` at
  your real metrics store: swap the impl.py bodies, keep the contracts.
- Tighten or relax the floor in `quality-gate` (and keep the scorer's
  calibration examples consistent with it).
- Feed real samples: wire an ADR 100 event trigger so each sampled
  production interaction POSTs one increment.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls per run** (scorer +
escalate/ack — the gate and both TOOL nodes are deterministic, zero-cost).
Cap absolute spend with the agent `budget.max_cost_usd_per_run` field or a
governance COST gate (ADR 093).
