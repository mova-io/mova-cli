# PII Detection pattern — governance

Topology: `TOOL redact-pii → DECISION(pii_found) → {TOOL quarantine | TOOL store-clean} → notify`

Document scanning + masking, durable on Temporal — **zero LLM calls on the
redaction, the routing, and both dispositions**. The entry `redact` TOOL node
(ADR 097) runs the workflow-local `redact-pii` python skill: three anchored
stdlib regexes masking emails / hyphenated US SSNs / US phone numbers to
`[EMAIL]`/`[SSN]`/`[PHONE]` tokens. Redaction is deliberately NOT an LLM's
job — a prompt can miss one SSN in a thousand runs and nobody can prove
which; the regexes either match or they don't, replay identically, and are
unit-tested character by character. A deterministic `decision` node
(ADR 094) routes on the skill's `pii_found` count: PII → the `sim-dlp` TOOL
records an auditable quarantine row; clean → `sim-store` records the clean
store. Both paths **converge on one notify agent** (exclusive convergence,
ADR 098) whose input schema deliberately EXCLUDES the original document — the
single LLM call in the workflow can only ever see masked text.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Deterministic redaction as a step | the `redact` TOOL node (ADR 097) — anchored regexes in `skills/redact-pii/impl.py`, schema-validated in and out, no model call. |
| Deterministic value routing | the `gate` `decision` node — `pii_found == true` → quarantine, else → clean store. No model call. |
| Auditable dispositions | `sim-dlp` / `sim-store` TOOL nodes record `{system: dlp, action: quarantine|store_clean}` ledger rows — the side-effect a suite (or an auditor) can count. |
| Data minimization for the LLM | the `notify` agent's input schema omits `document` — the model sees `redacted_text` only, enforced by the projection rule, not by prompt politeness. |
| Self-contained workflow-local skills | every skill carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow, no external package. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Masking is deterministic | three fixed regexes, fixed order, stdlib `re` only | the redaction replays identically on Temporal and its completeness is provable in unit tests, not vibes. |
| No-false-positive posture | bare 9/10-digit runs are NOT masked (only separated SSN/phone shapes) | silently corrupting clean documents is the worse failure mode for a masking system. |
| The LLM cannot leak what it never saw | notify's input schema excludes `document` | data minimization enforced structurally at the node boundary. |
| Dispositions are governed | both sim TOOLs declare `side_effects: mutates-state` and clear the SKILL gate (ADR 093/097) | the external write cannot hide behind a prompt. |

## Customize

- Extend the redactor: add a `(token, regex)` pair to `_RULES` in
  `skills/redact-pii/impl.py` (IBANs, card numbers, national IDs) — keep the
  no-false-positive posture and add boundary tests first.
- Point `sim-dlp`/`sim-store` at your real DLP/storage backend: swap the
  impl.py bodies, keep the schema contracts.
- Need a human review of quarantined documents? Insert a HUMAN gate with
  `routes` (ADR 099) between `quarantine` and `notify`.

## Budget

Per-run LLM spend is bounded: **exactly 1 model call per run** (notify — the
redactor, the gate routing, and both disposition TOOLs are deterministic,
zero-cost). Cap absolute spend with the agent `budget.max_cost_usd_per_run`
field or a governance COST gate (ADR 093); the eval-gate below is the
quality budget.
