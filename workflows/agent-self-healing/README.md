# agent-self-healing — certification deployable (scenario #16)

An agent detects its own degraded output quality and heals, durable on
Temporal:

```
TOOL health-check → DECISION(quality_score ≥ 0.8)
  ├─ healthy-report                              (healthy short-circuit)
  └─ diagnose → TOOL apply-fix → DECISION(fix_status)
       ├─ verify-report                          (fix applied, healed)
       └─ [HUMAN escalate, ack] → incident-report (fix failed — drift)
```

- **health-check** (TOOL, ADR 097): `sim-health-check` returns canned
  deterministic quality metrics for `input.agent_name` and records a
  `{system: monitor, action: health_check}` sim-ledger row.
- **quality-gate / verify** (DECISION, ADR 094): pure predicates over the
  measured score / the fix outcome — no LLM in the control path.
- **apply-fix** (TOOL, ADR 097): `sim-apply-fix` deterministically applies
  the diagnosed fix (a "drift" symptom FAILS — the one fault class
  self-healing cannot fix) and records `{system: agent_registry, action:
  apply_fix}`.
- **escalate** (HUMAN, ADR 099): the failed-fix path pauses durably for an
  operator `ack`; any other wording falls back to the same incident report.
- **No cycles**: mdk workflows are acyclic by construction — a failed fix
  can only escalate, never retry-loop.

Three coherent copies (keep them byte-identical where shared — see
`tests/test_b9_scenarios.py`): this deployable,
`certification/scenarios/agent-self-healing/` (suite mirror + cases.yaml),
and `src/movate/templates/pattern_agent_self_healing/` (`mdk init --pattern
agent-self-healing`).

Validate / spot-run:

```sh
mdk validate workflows/agent-self-healing
mdk run workflows/agent-self-healing/agents/diagnose \
  '{"agent_name": "invoice-parser", "quality_score": 0.55, "symptom": "elevated output-schema validation failures on recent runs"}'
```
