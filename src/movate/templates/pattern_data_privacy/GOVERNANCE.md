# Data Privacy pattern — governance

Topology: `classify → DECISION(classification) → {TOOL redact → TOOL audit-store | TOOL audit-store} → summary`

Classify → policy-route → **audited** storage, durable on Temporal. The
compliance story: **every path leaves an audit row** — the
classification-keyed `{system: dlp, action: store_<classification>}` ledger
entry recorded by the shared `sim-audit-store` TOOL node (ADR 097) that all
three routes converge on (exclusive convergence, ADR 098). The ONE judgment
call is the `classify` agent — calibrated with explicit per-class examples
and enum-pinned to `public | internal | regulated` by its output schema —
while the ROUTING on that judgment is a deterministic `decision` node
(ADR 094): regulated documents detour through the `redact-pii` TOOL (three
anchored regexes, no LLM — the same skill the pii-detection pattern ships)
so no model downstream of the route ever sees an unmasked identifier.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Calibrated, schema-pinned classification | `agents/classify` — prompt carries one explicit example per class + a "when in doubt, more restrictive" rule; the output schema enum makes a fourth label impossible. |
| Deterministic policy routing on an LLM judgment | the `route` `decision` node (ADR 094) — the model judges once, the route table is pure. |
| Deterministic redaction on the regulated leg | the `redact` TOOL node (ADR 097) — `skills/redact-pii/impl.py`, unit-tested regexes, no model call. |
| A compliance trail no path can skip | the shared `audit-store` TOOL node — all three classifications converge on it; the action is derived 1:1 from the enum-pinned classification (an out-of-vocabulary value fails the skill's input validation loudly). |
| Self-contained workflow-local skills | both skills carry their own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| The label vocabulary is closed | the classify output schema enum + the skill input schema enum | a creative label cannot invent an unaudited storage bucket — it fails schema validation instead. |
| Doubt escalates, never relaxes | the prompt's explicit tie-break rule (public < internal < regulated) | misclassification risk is biased toward the SAFER (more restrictive) handling. |
| Regulated data is masked before any further LLM | the route detours regulated documents through `redact` first; the summary agent's input schema omits `document` | data minimization is structural, not prompt politeness. |
| Storage is governed | `sim-audit-store` declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) | the external write cannot hide behind a prompt. |

## Customize

- Extend the taxonomy: add a label to BOTH enums (classify output schema +
  sim-audit-store input schema) and a calibration example to the prompt —
  the route table and the audit action follow from the label.
- Point `sim-audit-store` at your real records-management backend: swap the
  impl.py body, keep the schema contract (and the classification-keyed
  action — that 1:1 mapping IS the audit trail).
- Need a human check on regulated documents? Insert a HUMAN gate with
  `routes` (ADR 099) between `redact` and `audit-store`.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls per run** (classify +
summary — the routing decision, the redact TOOL, and the audit-store TOOL
are deterministic, zero-cost). Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093); the
eval-gate below is the quality budget.
