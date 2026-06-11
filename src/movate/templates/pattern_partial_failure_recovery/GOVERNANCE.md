# Partial Failure Recovery pattern — governance

Topology: `TOOL step-one → TOOL step-two (flaky, retries ⟳3) → TOOL step-three → notify`

A three-step pipeline, durable on Temporal, whose middle step is flaky by
design — the live proof that **completed steps are never re-executed when a
later step fails**. `step-one`/`step-three` call the shared parameterized
`sim-step` skill (the ADR 097 D1 `input:` literal map names the step;
`output_key` namespaces the deltas), each recording exactly one
`{system: pipeline, action: step1|step3}` ledger row. `step-two`
(`sim-step-flaky`) records one `step2_attempt` row per invocation, raises
while attempts <= `fail_times`, and records the single `step2` row only on
the invocation that succeeds. With `fail_times: 1` the ledger reads
step1 ×1, step2_attempt ×2, step2 ×1, step3 ×1 — Temporal replayed only the
FAILED activity under the compiled `_RETRY_POLICY` (`maximum_attempts=3`,
ADR 054 D9); the completed step's result came back from history.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Completed steps survive a later failure | step-one's single ledger row next to step-two's two attempt rows — the recovery proof read from the audit trail. |
| Per-activity (not per-workflow) retries | Temporal re-runs ONLY the failed `step-two` activity; steps one and three execute exactly once. |
| Exactly-once business effects under retry | the `step2` success row lands once however many attempts the step burned. |
| Parameterized workflow-local skills | ONE `sim-step` skill serves two stages via the ADR 097 D1 `input:` literal map — no copy-paste skill per stage. |
| Collision-safe step outputs | `output_key: step1|step2|step3` namespaces each stage's delta (ADR 097 D1) — three results from two skills, zero key collisions. |
| Deterministic skill execution as steps | all three TOOL nodes (ADR 097) — `dispatch_skill` calls, schema-validated in and out, SKILL-gate governed, no LLM. |
| Self-contained workflow-local skills | `skills/sim-step/` + `skills/sim-step-flaky/` carry their own `impl.py` next to `skill.yaml` — bake into a worker image with the workflow. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Retries are capped | `_RETRY_POLICY (maximum_attempts=3)` on every activity | a permanently-broken middle step costs exactly 3 attempts, then fails the pipeline loudly. |
| Recovery is auditable | one attempt row per step-two invocation + one row per completed stage | "which steps re-ran" is answerable from the ledger, not from Temporal internals. |
| Order is structural | the three TOOL nodes chain by explicit edges | step-three cannot run before step-two's success; nothing is skipped on recovery. |
| Every step is governed | all skills declare `side_effects: mutates-state` and clear the SKILL gate (ADR 093/097) statically and at runtime | the pipeline's writes cannot hide behind a prompt. |

## Customize

- Point the `sim-step` impl at your real pipeline stages (extract, load,
  publish, …): keep the one-row-per-stage contract and the recovery story
  stays auditable.
- Add stages by adding TOOL nodes with new `step: {literal: ...}` literals
  and `output_key`s — the skill is already parameterized.
- Keep flakiness simulation (`sim-step-flaky`) in pre-prod bundles to
  rehearse recovery; swap it for the real middle stage in production.

## Budget

Per-run LLM spend is bounded: **exactly 1 model call on every path** (the
final notify — all three pipeline steps are deterministic, zero-cost; a
retry re-runs only the zero-cost skill activity, never an LLM call). Cap
absolute spend with the agent `budget.max_cost_usd_per_run` field or a
governance COST gate (ADR 093); the eval-gate below is the quality budget.
