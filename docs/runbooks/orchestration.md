# Orchestration runbook — scheduler, triggers, durable/HITL

The orchestration substrate (ADR 017) is the **native** engine `mdk` already
has — the workflow DAG, the Postgres job queue, and the KEDA worker — plus three
gaps it closes: **scheduling**, **event/webhook triggers**, and **durable +
human-in-the-loop (HITL)** execution. There is no external orchestrator and no
in-process timer daemon: schedules and triggers turn into ordinary
`JobKind.AGENT` / `JobKind.WORKFLOW` jobs on the existing queue, so they inherit
retry / dead-letter / observability for free.

This runbook is for the operator running a deployed movate. For deployment
itself see [`../azure-bootstrap.md`](../azure-bootstrap.md).

---

## 1. Scheduler — run an agent/workflow on a cron

### Model

* `mdk schedule set|list|clear` records a **schedule** in storage (no job runs
  at record time).
* An **external cron** periodically runs `mdk scheduler-tick`. The tick finds
  schedules whose cadence has elapsed and enqueues one job per due schedule,
  then stamps `last_enqueued_at` so it won't re-enqueue inside the cadence
  window.
* The KEDA worker drains the queue and executes the jobs.

On Azure the cron is a **Container Apps Job** (`containerapp-scheduler.bicep`),
gated by two `main.bicep` params:

| param | default | meaning |
|---|---|---|
| `enableScheduler` | `false` | deploy the scheduler Job. Requires `enableApiWorker = true` (it shares the worker image/identity and enqueues into the queue the worker drains). |
| `schedulerCron` | `*/5 * * * *` | 5-field UTC cron for the Job. The tick is idempotent and only enqueues due schedules, so running more often than the finest cadence is safe. |

`mdk scheduler-tick` is the **unified** entrypoint — it drains BOTH the generic
agent/workflow schedules (ADR 017) and the continuous-eval schedules (ADR 016)
in one pass. The older `mdk eval-scheduler-tick` still exists and ticks
eval-only, for back-compat.

### Configure a schedule

```bash
# Run an agent every 6h with a fixed input
mdk schedule set faq-agent --cadence 6h --input '{"text": "daily digest"}'

# Run a workflow nightly, named, notify when an enqueued job finishes
mdk schedule set returns-pipeline --kind workflow --cadence 1d \
    --name nightly-returns --notify-email me@co.com

# Create a schedule but leave it dormant (no enqueue until enabled)
mdk schedule set faq-agent --cadence 30m --disabled
```

`set` options (verified against `src/movate/cli/schedule_cmd.py`):

| flag | notes |
|---|---|
| `--cadence` (required) | bare int seconds OR a duration suffix: `s`/`m`/`h`/`d` (e.g. `30m`, `6h`, `1d`). Must be positive. |
| `--kind` / `-k` | `agent` (default) or `workflow`. Anything else exits 2 — eval has its own scheduler (`mdk eval-schedule`). |
| `--name` | schedule handle (unique per tenant). Defaults to the resolved target name. |
| `--input` / `-i` | job payload: JSON object string, a file path, or `-` for stdin. Default `{}`. |
| `--disabled` | dormant — recorded but never enqueues. |
| `--notify-email` | email to notify when an enqueued job finishes. |
| `--format` | `table` (default) or `json`. |

List / clear:

```bash
mdk schedule list                 # table: name, kind, target, cadence, enabled, last enqueued
mdk schedule list --format json
mdk schedule clear nightly-returns
```

### Drive the tick (off-Azure / manual)

```bash
mdk scheduler-tick                # one pass; "enqueued N job(s); skipped M not-yet-due"
mdk scheduler-tick --format json  # {"enqueued": [...], "skipped": [...]}
```

Any cron (crontab, systemd timer, k8s CronJob) that runs `mdk scheduler-tick`
every few minutes works — the Azure Container Apps Job is just one such cron.

### Troubleshoot "my schedule isn't firing"

1. **Is the schedule enabled?** `mdk schedule list` — an `enabled: no` row was
   created with `--disabled` and never enqueues.
2. **Has its cadence elapsed?** The `last enqueued` column plus the cadence is
   the next-due time. The tick deliberately *skips* a schedule until its window
   has lapsed (that's the "skipped N not-yet-due" line).
3. **Is the tick running?** On Azure, confirm `enableScheduler = true` AND
   `enableApiWorker = true` (the scheduler Job requires the worker). Check the
   Container Apps Job execution history. Off-Azure, confirm your cron actually
   invokes `mdk scheduler-tick`.
4. **Is a worker draining the queue?** The tick only *enqueues* — a job sits
   `queued` until the KEDA worker claims it. With no worker, schedules fire but
   nothing executes.
5. **Idempotency note:** the tick is safe to run more often than the cadence.
   It stamps each enqueue, so a 1-minute cron against a 6h schedule still
   enqueues only every 6h.

---

## 2. Event / webhook triggers — run on an external event

A **trigger** is the inbound-event sibling of a schedule: instead of firing on a
cron, it enqueues a job when an external system POSTs an event to a stable movate
webhook. The external caller has **no `mvt_*` API key** — it authenticates with a
**per-trigger secret** via an `X-Movate-Signature` HMAC over the raw request
body.

### Create a trigger

```bash
# Fire a ticket-triage agent on an inbound ticket webhook
mdk trigger create triage-agent --name zendesk-ticket \
    --input-defaults '{"source": "zendesk"}'

# A dormant workflow trigger
mdk trigger create returns-pipeline --kind workflow --disabled
```

`create` prints, **once and irrecoverably**, the webhook path, the secret +
salt (to stderr with a "save now" warning), and a copy-paste signed `curl`
example. Options (from `src/movate/cli/trigger_cmd.py`):

| flag | notes |
|---|---|
| `--kind` / `-k` | `agent` (default) or `workflow`. |
| `--name` | trigger handle (unique per tenant). Defaults to the target name. |
| `--input-defaults` / `-i` | default payload merged **under** the event body (the event body wins on key collisions). JSON object / file / `-`. Default `{}`. |
| `--disabled` | dormant — won't fire. |
| `--format` | `table` (default) or `json`. In JSON mode the secret + salt are still emitted once for scripted capture. |

```bash
mdk trigger list            # table: name, kind, target, trigger_id, enabled, last fired (no secrets)
mdk trigger delete zendesk-ticket
```

### The fire endpoint

`POST /api/v1/triggers/{trigger_id}/events` (route in
`src/movate/runtime/app.py`). It is intentionally **outside the API-key auth
dependency** — there is no `_scope(...)` on it. Auth is the per-trigger HMAC:

* **`X-Movate-Signature: sha256=<hex>`** — HMAC-SHA256 of the **raw request
  body**, keyed by the trigger's signing key (`hash_secret(secret, salt)`). The
  secret never travels on the wire.
* **`X-Movate-Delivery-Id: <id>`** (optional) — idempotency key (the GitHub
  `X-GitHub-Delivery` convention). A repeated delivery of the same id for this
  trigger returns the **same** `job_id` with `deduplicated: true`, does not
  enqueue a second job, and does not re-stamp `last_fired_at`. Capped at 200
  chars; empty/over-long is treated as absent (always-enqueue).

The raw body must be a JSON object (or empty → `{}`); it is merged **over** the
trigger's `input_defaults` to form the job input. A `JobKind.AGENT` /
`WORKFLOW` job is enqueued scoped to the **trigger's** tenant, returning **202**
`{job_id, status, deduplicated}`.

### Send a signed request

```bash
BODY='{"text": "hello"}'
SECRET='<the secret printed at create time>'
SALT='<the salt printed at create time>'

# The signing key is hash_secret(secret, salt). The CLI prints the exact key
# and an openssl one-liner in the `mdk trigger create` output — copy that.
# Conceptually:
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SIGNING_KEY" | sed 's/^.* //')

curl -X POST https://<your-runtime>/api/v1/triggers/<trigger_id>/events \
  -H "X-Movate-Signature: sha256=$SIG" \
  -H "X-Movate-Delivery-Id: ticket-4821" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

> Use the exact `curl` block `mdk trigger create` emits — it computes the
> signing key for you and shows the expected `sha256=` for the example body, so
> you can confirm your HMAC implementation matches before wiring a real webhook.

### Troubleshoot the fire endpoint

| Symptom | Cause / fix |
|---|---|
| **404** | Unknown OR **disabled** trigger. Existence is never leaked to an unauthenticated caller, so a disabled trigger looks identical to a non-existent one. Check `mdk trigger list` for `enabled: yes`. |
| **401** | Missing or invalid `X-Movate-Signature`. The HMAC must be over the **raw bytes** you send (not a re-serialized/whitespace-normalized copy), keyed by the signing key, hex-encoded, prefixed `sha256=`. Re-derive with the `curl` example from `create`. |
| **400** | Body present but not a JSON object. |
| Replay / duplicate jobs | Send a stable `X-Movate-Delivery-Id` per logical event. Retries with the same id dedup to one job (`deduplicated: true`). Without the header, every valid POST enqueues. |
| Job enqueued but nothing runs | Same as the scheduler — a worker must be draining the queue. |

---

## 3. Durable + HITL — pause a workflow on a human gate, then resume

A workflow can pause at a `HUMAN` node (ADR 017 D5). The runner persists a
durable **PAUSED** `WorkflowRunRecord` (the gate prompt + the state keys the
human must supply via the gate's `output_contract`) and stops. An operator finds
these paused runs and signals a decision to resume.

`mdk workflow` talks to a **deployed runtime** over HTTP (`/api/v1/workflow-runs`)
via `MovateClient` — same target resolution as `mdk jobs` (`--target` >
top-level `-t`/`MOVATE_TARGET` > active config target).

### Find paused runs (the HITL queue)

```bash
# Paused runs awaiting a human decision; prints each gate's prompt + needed keys
mdk workflow runs --paused

# All workflow runs, newest first, pipe-friendly
mdk workflow runs --output json | jq '.workflow_runs[].workflow_run_id'
```

`runs` options (from `src/movate/cli/workflow_cmd.py`):

| flag | notes |
|---|---|
| `--paused` | narrow to PAUSED runs (the HITL queue). Maps to `?status=paused`. |
| `--limit` / `-n` | max rows (default 20; server caps at 100). |
| `--target` / `-t` | deployment target name. |
| `--output` / `-o` | `table` (default) or `json`. |

Backed by `GET /api/v1/workflow-runs` (scope `read`, tenant-scoped). Each PAUSED
row surfaces its `human_task` (prompt + `output_contract`).

### Signal a decision to resume

```bash
# Approve and continue — the decision must include every output_contract key
mdk workflow signal 1f3c... --decision '{"decision": "approve"}'

# Decision from a file
mdk workflow signal 1f3c... -d ./approval.json
```

Backed by `POST /api/v1/workflow-runs/{id}/signal` (scope **`run`**,
tenant-scoped). The runtime:

1. Loads the run (**404** if missing / other tenant).
2. **409** if the run is not awaiting a signal (already resumed / terminal) —
   this is the idempotency guard against a double-resume.
3. **422** if the decision omits a key the gate's `output_contract` requires.
4. Merges the decision into the paused checkpoint (decision wins).
5. Enqueues a **continuation `JobKind.WORKFLOW` job** carrying
   `resume_workflow_run_id` and returns **202** `{job_id, status}`.

Critically, the signal endpoint does **not** run the workflow inline — the
**worker** resumes the runner from the gate's successor. Poll the continuation
job:

```bash
mdk jobs wait <job_id>
```

### Troubleshoot HITL

| Symptom | Cause / fix |
|---|---|
| `mdk workflow runs --paused` is empty | No run has hit a `HUMAN` gate, or they were already signalled. Drop `--paused` to see SUCCESS/ERROR/PAUSED across all states. |
| Signal returns **422** | The `--decision` object is missing a required `output_contract` key. The needed keys are shown in `mdk workflow runs --paused` (`needs: …`). |
| Signal returns **409** | The run is no longer PAUSED — it was already signalled (a continuation is in flight) or it reached a terminal state. A second signal is refused by design. |
| Signal returns **404** | Wrong `workflow_run_id`, or it belongs to another tenant (cross-tenant access 404s, never 403). |
| Signalled but no progress | The continuation job is queued — confirm a worker is draining the queue, then `mdk jobs wait <continuation_job_id>`. |

---

## See also

* [`improvement-loop.md`](improvement-loop.md) — continuous eval shares the same
  scheduler primitive (`mdk eval-schedule` + `mdk eval-scheduler-tick`).
* [`serving-and-keys.md`](serving-and-keys.md) — job/run polling, batch, auth
  scopes that gate these endpoints.
* ADR 017 (`../adr/017-agent-orchestration.md`), ADR 016
  (`../adr/016-continuous-improvement-loop.md`).
