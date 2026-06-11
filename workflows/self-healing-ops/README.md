# self-healing-ops — certification deployable (scenario #26)

Infrastructure self-healing with two-attempt remediation, durable on
Temporal:

```
TOOL detect → triage → TOOL remediate-1 → DECISION(r1_status)
  ├─ closure                                  (fixed on attempt 1)
  └─ TOOL remediate-2 → DECISION(r2_status)
       ├─ closure                             (fixed on the retry)
       └─ [HUMAN escalate, ack] → closure     (both attempts failed)
```

- **detect** (TOOL, ADR 097): `sim-detect` maps `input.signal` to the canned
  fault catalog and records a `{system: monitor, action: detect}` sim-ledger
  row.
- **triage** (the one LLM judgment): enum-pinned severity + the recommended
  remediation action; applying it stays deterministic.
- **remediate-1 / remediate-2** (TOOL, ADR 097): "retry on failure" is
  UNROLLED — mdk workflows have no cycles, so the retry is a second
  sequential TOOL node (`sim-remediate-retry`). Each attempt writes its own
  `{system: ops, action: remediate}` row (`attempt: 1` / `attempt: 2`).
  Deterministic outcomes: transient ("stuck") faults fail attempt 1 and land
  on the retry; "hardware" faults fail BOTH and escalate.
- **escalate** (HUMAN, ADR 099): durable pause for an operator `ack`; any
  other wording falls back to the same closure.
- **closure** (ADR 098 exclusive convergence): ONE shared tail for all three
  exits — its prompt guards `r2_status`/`decision`, the keys only some paths
  set.

Three coherent copies (keep them byte-identical where shared — see
`tests/test_b9_scenarios.py`): this deployable,
`certification/scenarios/self-healing-ops/` (suite mirror + cases.yaml), and
`src/movate/templates/pattern_self_healing_ops/` (`mdk init --pattern
self-healing-ops`).

Validate / spot-run:

```sh
mdk validate workflows/self-healing-ops
mdk run workflows/self-healing-ops/agents/triage \
  '{"signal": "checkout-latency-spike", "fault": "connection pool exhaustion", "component": "checkout-api"}'
```
