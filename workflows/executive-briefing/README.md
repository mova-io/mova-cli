# executive-briefing — scheduled multi-source executive digest (scenario #12)

Durable Temporal workflow producing the daily executive digest from two
simulated business sources. Topology:

```
TOOL gather-metrics → TOOL gather-incidents → digest (LLM)
    → DECISION(risk_count) → {flag | archive}
```

* **Sequential gather chain (ADR 097).** Entrypoint fan-out is not available
  for TOOL nodes, so the two source pulls run as a SEQUENTIAL chain of TOOL
  nodes. Each workflow-local python skill returns canned, replay-identical
  data (keyed by the `profile` input knob, `steady` | `degraded`) and records
  one auditable row to the `sim_side_effects` ledger — `{system: metrics,
  action: gather}` and `{system: incidents, action: gather}`. No network IO
  anywhere; point the impls at your real metrics/incident backends to go
  live (keep the schema contracts).
* **One composing LLM call.** The `digest` agent writes the briefing
  STRICTLY from the two gathered results — headline, sections, risk_flags,
  and the `risk_count` integer downstream routing keys off.
* **Deterministic risk routing (ADR 094).** `risk_count > 0` escalates to
  the `flag` agent; a clean digest files via `archive`. No LLM in the
  control path.

## Cron-born (ADR 100)

This workflow is designed to be **started by the clock**, not by a human at
a keyboard: ADR 100 D1 cron schedules enqueue the exact same JobRecord a
manual `POST /run` produces, so the definition carries nothing
schedule-specific. The production binding (07:00 weekdays, US Eastern):

```sh
# ADR 100 D1 — cron schedule feeding the {"period": "daily"}-style input.
# DO NOT create this schedule as part of certification: the suite drives the
# workflow via POST /run; this binding is production wiring only.
mdk schedule set executive-briefing -k workflow \
    --cron "0 7 * * 1-5" --tz America/New_York \
    --input '{"period": "daily"}'
```

The `profile` knob defaults to `steady` when absent (the cron input above
omits it); the certification cases pass `degraded` to drive the escalation
route deterministically.

## Certification

Mirrors `certification/scenarios/executive-briefing/` (cases: degraded →
flag route, steady → archive route; both assert the two gather ledger rows)
and ships as the `executive-briefing` pattern template
(`mdk init --pattern executive-briefing`).
