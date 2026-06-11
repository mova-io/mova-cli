# Long Running Research pattern — governance

Topology: `research → TOOL append → DECISION(increment) → {final-report | ack}`

Scheduled incremental research, durable on Temporal — the workflow shape for
**ADR 100 D1 cron schedules**. The long-running series is deliberately
inverted: instead of one workflow that sleeps for days between increments,
the cron schedule (`mdk schedule set <name> -k workflow --cron ...`) is the
durable outer loop and the workflow is the idempotent body — each scheduled
fire runs ONE increment (`input {topic, increment}`). The `research` agent
drafts this increment's findings; the `sim-append-findings` TOOL node
(ADR 097) appends the one `{system: research, action: append}` ledger row
(increment in the payload — the accumulating research log); a deterministic
`decision` node (ADR 094) routes `increment gte 3` into the `final-report`
agent and every earlier increment to the light `ack` agent. Jobs the
scheduler tick enqueues carry `origin="schedule:<name>"` (ADR 100 D4) for
provenance.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Cron-scheduled workflow execution | register once with `mdk schedule set ... --cron "0 7 * * *"`; the stateless `mdk scheduler-tick` cron entrypoint enqueues each fire (ADR 100 D1 — at most once per matched window, one catch-up after downtime, never a backfill storm). |
| Increment-shaped long-running work | each run is small, idempotent, and independently retryable — no week-long workflow to babysit, no durable timer marathon. |
| An accumulating, auditable work log | one append row per increment with the increment number in the payload — the series' progress reads straight out of the ledger. |
| Scheduled-run provenance | `origin="schedule:<name>"` on every tick-enqueued job (`GET /api/v1/jobs`) + the Temporal memo `mdk_origin` (ADR 100 D4). |
| Deterministic finality routing | the `finality-check` `decision` node (ADR 094) — a pure `gte` predicate over the increment; no LLM decides when the series ends. |
| Deterministic skill execution as a step | the `append` TOOL node (ADR 097) — one `dispatch_skill` call, schema-validated in and out, SKILL-gate governed, no LLM. |
| Self-contained workflow-local skill | `skills/sim-append-findings/` carries its own `impl.py` next to `skill.yaml` — bakes into a worker image with the workflow. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | the loop lives in the SCHEDULER (bounded by the cron + finality input), never in the graph. |
| One fire per window | the tick's `last_enqueued_at` idempotency (ADR 100 D1) | running the tick more often than the cron can never double-enqueue an increment. |
| Finality is deterministic | the `increment gte 3` predicate (ADR 094) | the series cannot run forever on a model's whim — the closing increment is a pure value comparison. |
| The log write is governed | the skill declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) statically and at runtime | the research log cannot be appended from behind a prompt. |
| Increment cost is flat | exactly two model calls per increment, regardless of series length | the SERIES' spend is the schedule's cadence x a constant — budget by tuning the cron. |

## Customize

- Move the series length: edit the `gte 3` predicate — no new nodes.
- Point `sim-append-findings` at your real research store (a KB, a wiki, a
  vector index): swap the impl.py body, keep the schema contract.
- Feed each increment richer context by extending the schedule's `--input`
  payload (it is the workflow's initial state, verbatim).
- Wrap-up distribution (email the final report) belongs in a downstream
  node after `final-report` — keep the report agent pure.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls on every path**
(research, then final-report or ack — the finality decision and the append
TOOL node are deterministic, zero-cost). The series' total spend is the
schedule cadence times that constant. Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093); the
eval-gate below is the quality budget.
