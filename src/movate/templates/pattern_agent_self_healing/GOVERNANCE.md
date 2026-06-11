# Agent Self-Healing pattern — governance

Topology: `TOOL health-check → DECISION(quality_score) → {healthy-report | diagnose → TOOL apply-fix → DECISION(fix_status) → {verify-report | [HUMAN ack] → incident-report}}`

An agent detects its own degraded output quality and heals, durable on
Temporal — **the monitor and the fix are never an LLM's job**. The entry
`health-check` TOOL node (ADR 097) measures the named agent's quality
deterministically (the workflow-local `sim-health-check` skill — canned
monitor data, one auditable `{system: monitor, action: health_check}` ledger
row); a deterministic `decision` node (ADR 094) routes the 0.8 threshold. A
degraded agent gets ONE LLM diagnosis (probable cause + the one fix action),
but the fix is APPLIED by the `apply-fix` TOOL node (`sim-apply-fix` — pure
predicate: a model-drift symptom deterministically FAILS, everything else
applies; one `{system: agent_registry, action: apply_fix}` row either way).
A second decision verifies the outcome: applied → verify-report; failed →
the run pauses durably at the `escalate` HUMAN gate (ADR 099 — operator
`ack`; any other wording falls back the same way) and files the incident
report. **No cycles**: a failed fix can only escalate, never retry-loop.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic self-measurement | the `health-check` TOOL node (ADR 097) — the quality score that decides the run's path is monitor data, not a model grading itself. |
| Deterministic quality + verification routing | the `quality-gate` / `verify` `decision` nodes (ADR 094) — pure predicates over the measured score and the fix outcome; no LLM in the control path. |
| Diagnosis without authority | the `diagnose` agent names cause + fix action, but `sim-apply-fix`'s outcome is a pure predicate over the SYMPTOM — the LLM cannot talk a fix into "applied". |
| Auditable healing | one `{system: monitor, action: health_check}` row per run + one `{system: agent_registry, action: apply_fix}` row per fix attempt, recorded by deterministic code. |
| Durable human-in-the-loop on the un-self-fixable path | the `escalate` HUMAN gate (ADR 099) pauses durably; the operator's `ack` routes to the incident report. |
| Self-contained workflow-local skills | `skills/sim-health-check/` and `skills/sim-apply-fix/` carry their own `impl.py` next to `skill.yaml` — they bake into a worker image with the workflow. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway heal-retry loop is impossible by construction — a failed fix can only escalate to a human. |
| The health check is deterministic | `sim-health-check` is canned data keyed off the agent name (closed catalog, loud KeyError outside it) | the routing decision replays identically on Temporal; an unknown agent can never be silently "healthy". |
| The fix outcome is deterministic | `sim-apply-fix`'s applied/failed is a pure predicate over the symptom | the diagnosis (an LLM output) is recorded for the audit trail but never decides the outcome. |
| Escalation cannot be skipped | `incident-report` is reachable ONLY via the HUMAN gate, and the gate is reachable only on a failed fix | the graph, not a convention, guarantees a human sees every un-self-fixable fault. |
| Prose answers fail safe | the gate's `fallback: incident-report` (ADR 099) | however the operator words the acknowledgment, the run can only land on the incident report. |
| The fix is governed | both skills declare `side_effects: mutates-state` and clear the SKILL gate (ADR 093/097) | the registry write cannot hide behind a prompt. |

## Customize

- Point `sim-health-check` at your real monitor (eval-gate scores, error
  rates, drift detectors): swap the impl.py body, keep the
  `quality_score` + `symptom` contract.
- Point `sim-apply-fix` at your real agent registry (redeploy, repin,
  reconfigure): keep the outcome deterministic — derive `fix_status` from
  the registry's response, never from the diagnosis text.
- Tune the threshold: the `quality-gate` case (`gte 0.8`) is the autonomy
  boundary — raise it to heal earlier, lower it to tolerate more drift.
- Want a second fix attempt before escalating? Unroll it (the
  self-healing-ops pattern shows the shape): clone `apply-fix` + `verify`
  behind the failed leg — keep the bound, never a loop.

## Budget

Per-run LLM spend is bounded: **at most 2 model calls per run** (diagnose +
verify-report/incident-report on the degraded paths; the healthy path makes
exactly 1 — the health-check and apply-fix TOOL nodes, both decision gates,
and the HUMAN routing are deterministic, zero-cost). Cap absolute spend with
the agent `budget.max_cost_usd_per_run` field or a governance COST gate
(ADR 093); the eval-gate below is the quality budget.
