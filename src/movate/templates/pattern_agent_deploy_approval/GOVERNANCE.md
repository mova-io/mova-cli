# Agent Deploy Approval pattern — governance

Topology: `TOOL eval-run → DECISION(score ≥ 0.85) → [HUMAN routes approve|reject] → TOOL promote → notify | rejected`

Eval-gated promotion with a HUMAN release gate, durable on Temporal.
**Nothing reaches the model registry without two independent green lights**:
a deterministic eval gate and a human approver. The `eval-runner` TOOL node
(ADR 097) produces the eval evidence — in this template a calibrated
SIMULATION keyed by the enum-pinned `fixture` input (real `mdk eval` runs
are too heavy to drive inside a workflow activity; swap the impl to call
your eval harness) — and records the auditable `{system: eval, action: run}`
ledger row with the scores. The `score-gate` decision node (ADR 094) applies
the 0.85 threshold as a pure numeric predicate: a below-threshold candidate
lands on `rejected` WITH the eval report (rejected-with-report) and can
never reach the human. A passing candidate still promotes nothing until the
`promote-approval` HUMAN gate routes its own structured decision (ADR 099).
Promotion itself is the `sim-promote` TOOL node — one `{system: registry,
action: promote}` row per approved run, recorded by deterministic code.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Eval evidence before judgment | the `eval-run` TOOL node runs FIRST and its scores + report are in state when the human reads the gate prompt. |
| Deterministic quality gating | the `score-gate` decision node (ADR 094) — a pure `gte 0.85` predicate; no LLM decides what passes. |
| Durable human release gate | the `promote-approval` HUMAN node (ADR 099) — pauses durably, trim+casefold exact match, fail-safe fallback. |
| Auditable promotion | the `promote` TOOL node (ADR 097) — one registry ledger row per approved run. |
| Rejected-with-report | every rejected exit converges on ONE `rejected` agent (ADR 098) that relays the eval report (or the human veto) to the release owner. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| A failing eval cannot be promoted | `promote` is reachable only via the gate, the gate only via `score-gate`'s passing case | the graph, not a convention, blocks regressions. |
| The fixture vocabulary is closed | eval-runner's input-schema enum + a KeyError backstop in impl.py | an unknown fixture fails loud, never scores silently. |
| Prose answers fail safe | the gate's `fallback: rejected` (ADR 099) | "ship it I guess" rejects rather than promotes. |
| Promotion is governed | `sim-promote` declares `side_effects: mutates-state` (ADR 093/097) | the registry write cannot hide behind a prompt. |

## Customize

- Point `eval-runner` at your real eval harness: swap the impl.py body
  (call your eval API, map its metrics to `eval_score`/`eval_report`), keep
  the schema contract and the ledger row.
- Point `sim-promote` at your real registry the same way.
- Tune the threshold in `score-gate` (and mention it in the gate prompt so
  the approver knows what already passed).

## Budget

Per-run LLM spend is bounded: **at most 1 model call per run** (notify OR
rejected — the eval TOOL, the score gate, the HUMAN routing, and the promote
TOOL are all deterministic, zero-cost). Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093).
