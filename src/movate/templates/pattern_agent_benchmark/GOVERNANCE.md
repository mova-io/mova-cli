# Agent Benchmark pattern — governance

Topology: `candidate-a → candidate-b → compare → TOOL record-benchmark → notify`

Two-config benchmark of the SAME task, durable on Temporal. The task runs
SEQUENTIALLY through two candidate agents that share the same prompt TEXT
but differ in agent.yaml model params (temperature 0.0 vs 0.9 — the configs
under test), each writing its own response key so both survive in state. A
`compare` judge agent then scores BOTH responses (0-1 each — REQUIRED
output keys, so the judge cannot ignore one) and picks the enum-pinned
winner (`a`|`b`, no ties). The verdict is recorded by the
`sim-record-benchmark` TOOL node (ADR 097): one auditable `{system: eval,
action: benchmark}` ledger row per run.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Config-isolated comparison | two separate agent dirs, identical prompt text, different model params — the config delta is the ONLY variable. |
| A judge that must engage with both | `score_a` and `score_b` are required output-schema keys; `winner` is enum-pinned (a|b). |
| Auditable verdicts | the `record-benchmark` TOOL node (ADR 097) — the verdict lands in the eval ledger deterministically, no LLM between judge and record. |
| Honest assertions | the certification cases assert the CONTRACT (winner present, both scores present, the row recorded), never a specific winner — a winner is an LLM judgment. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No ties, no third verdict | the compare agent's `winner` enum | a creative verdict cannot derail the record. |
| Sequential, not parallel | plain edges (the ADR 092 diamond is deliberately not used) | nothing here needs concurrency; determinism of shape beats latency. |
| The judge sees blind responses | the compare prompt names candidates only as A and B | the judge cannot favor a config it cannot identify. |

## Customize

- Benchmark different MODELS (not just params): change `model.provider` in
  one candidate's agent.yaml — keep the prompt text identical, and extend
  the bundled policy's `allowed_providers` if you leave openai/anthropic.
- Add a third candidate: clone the agent dir with a `response_c` key and
  extend the compare agent's schema/prompt.
- Point `sim-record-benchmark` at your real experiment store: swap the
  impl.py body, keep the schema contract.

## Budget

Per-run LLM spend is bounded: **exactly 4 model calls per run** (two
candidates + the judge + notify — the record TOOL node is deterministic,
zero-cost). Cap absolute spend with the agent `budget.max_cost_usd_per_run`
field or a governance COST gate (ADR 093).
