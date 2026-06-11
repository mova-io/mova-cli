# long-running-research — scheduled incremental research (scenario #5)

The certification dogfood of **ADR 100 D1 cron schedules**. The
"long-running" shape is deliberately inverted: instead of one workflow that
sleeps for days between increments, **the cron schedule is the durable outer
loop and the workflow is the idempotent body** — each scheduled fire runs ONE
increment (`input {topic, increment}`): the `research` agent drafts this
increment's findings, the `sim-append-findings` TOOL node appends the one
`{system: research, action: append}` ledger row (increment in the payload —
the accumulating research log), and a deterministic decision routes
`increment gte 3` into the `final-report` agent (earlier increments get the
light `ack`).

## Registering the schedule

Local CLI (the row lands in the storage the CLI sees — `MOVATE_PG_URL` /
`MOVATE_DB_URL`, else the local SQLite file):

```bash
mdk schedule set long-running-research -k workflow \
  --cron "0 7 * * *" \
  --input '{"topic": "vector databases", "increment": 1}'
```

Against the deployed runtime (the row lands in the deployed Postgres,
tenant-scoped to the API key):

```bash
curl -X PUT "$MDK_DEV_API_URL/api/v1/schedules/long-running-research" \
  -H "Authorization: Bearer $MDK_DEV_KEY" -H "Content-Type: application/json" \
  -d '{"kind": "workflow", "target": "long-running-research",
       "cron": "0 7 * * *",
       "input": {"topic": "vector databases", "increment": 1}}'
```

The schedule fires via the stateless cron entrypoint — `mdk scheduler-tick`
(Azure: a Container Apps Job runs it on a cron trigger; there is no
in-process timer daemon). Each job the tick enqueues carries
`origin="schedule:long-running-research"` (ADR 100 D4), visible on
`GET /jobs/{id}` / `GET /api/v1/jobs` and as the Temporal memo `mdk_origin`.

## Why there is no schedule-driven certification case (the honest gap)

The brief asked whether a case could *create a schedule via the dev API, run
one `mdk scheduler-tick` against it, and assert a run with
`origin="schedule:..."` appears in the facts endpoint*. Investigated
(`movate/core/scheduler.py`, the `/api/v1/schedules` endpoints, the ADR 096
fact model) — the answer is no, for three concrete reasons:

1. **The tick has no API surface.** `PUT /api/v1/schedules/{name}` only
   persists the row; the enqueue happens when an external cron runs
   `mdk scheduler-tick` *in a process whose storage IS the deployed
   Postgres*. A driver case would have to shell out to the CLI with
   `MOVATE_PG_URL` set — an out-of-band **write** coupling the driver
   deliberately doesn't have (its only DB coupling today is the read-only
   sim-ledger assert, which honestly SKIPs when the DSN is absent).
2. **Facts don't carry origin.** `ObservabilityFact` (ADR 096,
   `extra="forbid"`) has no `origin` column, and the dispatch edge does not
   copy `job.origin` into `fact.attributes`. ADR 100 D4 provenance is
   surfaced on `JobView.origin` (`GET /jobs/{id}`, `GET /api/v1/jobs`) and
   the Temporal memo — so "origin appears in the facts endpoint" is
   unimplementable as specified.
3. **It isn't an append-style case extension.** The driver's case shape is
   declarative submit→hitl→expect with the driver doing the `POST /run`. A
   schedule case needs a setup phase (PUT the schedule), an out-of-band tick
   subprocess, and an inverted launch (DISCOVER the job the tick enqueued —
   e.g. `GET /api/v1/jobs?agent=long-running-research` filtered on
   `origin` — instead of submitting one). That is a new case shape, not a
   field.

**Follow-up that would close the gap honestly:** (a) stamp `job.origin` into
the workflow fact's `attributes` at the dispatch edge (additive, ADR 096's
bounded escape hatch — facts can then answer "which runs came from
schedules" without a join), and/or (b) a driver `schedule:` case phase that
PUTs the row and asserts the NEXT in-env tick's job via `GET /api/v1/jobs`
origin filtering. Both are deliberate non-goals for this batch (one PR = one
responsibility).

The two cases in `cases.yaml` certify the increment body itself — the thing
every scheduled fire executes — via the driver's normal `POST /run` path:
one mid-series increment (append row, ack, no final report) and the closing
increment (append row + final report).
