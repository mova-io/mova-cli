# A/B Testing pattern — governance

Topology: `TOOL assign-variant → DECISION(variant) → {variant-a | variant-b} → TOOL record-outcome → notify`

Deterministic A/B traffic split, durable on Temporal. The `assign-variant`
TOOL node (ADR 097) assigns the variant from the SHA-256 parity of the
`user_id` — NO randomness, and explicitly NOT Python's salted builtin
`hash()` (which would assign the same user different variants on different
workers and break Temporal replay determinism) — and records the auditable
`{system: ab, action: assign}` row. The `variant-gate` decision node
(ADR 094) routes to one of two experiment arms: BYTE-IDENTICAL prompt
files whose agent.yaml model params (temperature 0.0 vs 0.9) are the ONLY
difference — the config delta IS the experiment. Both arms write the same
`response` key on exclusive paths (ADR 098) and converge on the
`record-outcome` TOOL (`{system: ab, action: record_outcome}` row carrying
which variant served) then ONE notify agent.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic, sticky assignment | SHA-256 parity in assign-variant's impl.py — same user, same variant, every worker, every replay. |
| Single-variable experiments | byte-identical prompts, params-only delta — when outcomes differ you know why. |
| Auditable experiment trail | one assign row + one outcome row per run (ADR 097) — joinable by run_id for analysis. |
| Deterministic routing on a recorded value | the `variant-gate` decision node (ADR 094) reads the variant the ledger already witnessed. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| The variant vocabulary is closed | assign-variant's output enum (a|b) + the gate's two destinations | a third variant cannot appear mid-experiment. |
| No salted hashing | impl.py uses hashlib.sha256, documented in-module | builtin hash() varies per process (PYTHONHASHSEED) and would corrupt the split. |
| Arms cannot drift apart | prompt byte-equality is asserted by the structural test suite | the experiment stays single-variable. |

## Customize

- Change the split: weight by modulo buckets (e.g. % 10 < 3 → "a") in
  assign-variant's impl.py — keep it a pure function of user_id.
- Test different MODELS: change one arm's `model.provider` (keep prompts
  identical; extend the bundled policy's `allowed_providers` if needed).
- Point `sim-record-outcome` at your real experiment store: swap the
  impl.py body, keep the schema contract.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls per run** (one
variant arm + notify — the assign/outcome TOOL nodes and the variant gate
are deterministic, zero-cost). Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093).
