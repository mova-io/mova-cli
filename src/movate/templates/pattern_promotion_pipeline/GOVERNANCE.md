# Promotion Pipeline pattern — governance

Topology: `DECISION(stage) → {TOOL run-tests | TOOL stage-eval → [HUMAN signoff] | [HUMAN approval] → TOOL deploy} → notify | rejected`

Staged promotion gates in ONE workflow, durable on Temporal. A
deterministic `stage-gate` decision node (ADR 094) routes the CI-named
`stage` — `test` / `staging` / `production`, anything else fails safe to
`rejected` — onto three stage-routed paths (deliberately NOT a loop: each
run executes one stage; the CI system advances the stage between runs).
The gates escalate with the blast radius:

- **test** — fully automatic: the `sim-run-tests` TOOL (ADR 097) records
  one `{system: pipeline, action: stage_test}` row. No human.
- **staging** — evidence BEFORE judgment: the `sim-stage-eval` TOOL records
  the `{system: eval, action: stage_eval}` row, THEN the `staging-signoff`
  HUMAN gate routes its own structured decision (ADR 099) reading it.
- **production** — judgment BEFORE action: the `prod-approval` HUMAN gate
  comes FIRST, and the `sim-deploy` TOOL's `{system: pipeline, action:
  promote_prod}` row is reachable ONLY through its approve route.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Stage-proportional gating | no gate at test, sign-off after evidence at staging, approval before action at production. |
| Graph-enforced production safety | `deploy` has exactly one inbound edge: prod-approval's approve route — unapproved production writes are impossible by construction. |
| Deterministic stage routing | the `stage-gate` decision node (ADR 094) — a pure string match; unknown stages fail safe. |
| Auditable stages | every stage records exactly one ledger row (ADR 097); the certification cases assert each stage's row AND the absence of the other stages' rows. |
| Shared tails | one notify, one rejected (ADR 098) — three paths, no copy-paste terminal agents. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| The stage vocabulary is closed | the decision cases + fail-safe default | a typo'd stage rejects loudly rather than picking a gate. |
| Prose answers fail safe | both gates' `fallback: rejected` (ADR 099) | an out-of-vocabulary approval rejects rather than promotes. |
| One stage per run | the routed shape (not a loop) | a single run can never leapfrog test → production. |

## Customize

- Point the three skills at your real CI / eval harness / deploy tooling:
  swap the impl.py bodies, keep the schema contracts and ledger rows.
- Add a stage: one decision case + its tool/gate pair, converging on the
  shared tails.
- Chain stages automatically by having your CI submit the next stage's run
  on the previous one's success — the workflow stays one-stage-per-run.

## Budget

Per-run LLM spend is bounded: **at most 1 model call per run** (notify OR
rejected — the stage gate, both HUMAN routings, and all three TOOL nodes
are deterministic, zero-cost). Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093).
