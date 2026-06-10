# ADR 100 — Workflow triggers: cron schedules + inbound events converge on the existing job path

Status: Proposed
Date: 2026-06-09
Deciders: Engineering — additive fields on the existing `JobSchedule`/`Trigger`
surfaces (CLAUDE.md §7: extend the existing seam, don't add a second
workflow-start path).
Builds on: ADR 017 D2 (native scheduler + inbound triggers — the machinery this
ADR completes), ADR 035 (outbound lifecycle events/webhooks — the *other*
direction; `webhooks`/`webhook_attempts`/`webhook_cursors` are outbound
subscriptions, not inbound triggers), ADR 055/091 (workflow runtime dispatch
fork; Temporal as default runtime), ADR 088 (temporal workers load published
workflows), ADR 094 (the structured-over-DSL authoring posture this ADR's
input-mapping surface mirrors), ADR 016 (`EvalSchedule` — the domain-specific
schedule precedent).

## Context

The scheduled scenarios (long-running research, executive briefing, continuous
eval, KB refresh) and the event-driven scenarios (incident response, Azure
DevOps Service Hooks → work-item triage) — scenarios 5/7/12/13/21/23/27 of the
v1.0 scenario catalog — all reduce to one capability: **start a workflow (or
agent) without a human at a keyboard**, from a clock or from an inbound event.

Exploration finding: **the core machinery already exists and already supports
workflows.** ADR 017 D2 shipped both halves, and both converge on the one job
path:

- **Schedules.** `JobSchedule` (`core/models.py:3410`) carries
  `kind: agent|workflow` (validator at `core/models.py:3487-3503`), a `target`,
  a `cadence_seconds` interval, and the `input` payload ("the initial-state
  dict for workflows", `core/models.py:3469-3474`). `mdk schedule set <target>
  -k workflow --cadence 1d --input '{...}'` exists (`cli/schedule_cmd.py:91-136`),
  as do the `/api/v1/schedules` CRUD endpoints (`runtime/app.py:11959-12062`,
  `JobScheduleSubmission` at `runtime/schemas.py:1232` with the same
  agent|workflow validator at `:1267`). An external cron (an ACA Job on Azure)
  runs `mdk scheduler-tick`; `run_job_scheduler_tick`
  (`core/scheduler.py:285-322`) finds due rows and enqueues via
  `build_scheduled_job` (`core/scheduler.py:132-153`) — the exact `JobRecord`
  shape `POST /run` produces. Idempotent within a cadence window
  (`is_due`, `core/scheduler.py:83-98`); a stale-job reaper backstop rides the
  unified tick (`core/scheduler.py:352-365`).
- **Triggers.** `Trigger` (`core/models.py:3506`) likewise carries
  `kind: agent|workflow` + `target` + `input_defaults`. `mdk trigger create
  <target> -k workflow` exists (`cli/trigger_cmd.py:63-110`), as do
  `POST/GET/DELETE /api/v1/triggers` (`runtime/app.py:12101-12227`) and the
  unauthenticated-caller **fire endpoint**
  `POST /api/v1/triggers/{trigger_id}/events` (`runtime/app.py:12235-12357`):
  HMAC-SHA256 over the raw body (`X-Movate-Signature`,
  `core/triggers.py:157-196`), per-trigger secret hashed at rest like an API
  key (`mint_trigger`, `core/triggers.py:100-132`), 404-without-existence-leak
  for unknown/disabled triggers, and **replay dedup** on an optional
  `X-Movate-Delivery-Id` header via the `trigger_deliveries` table
  (`storage/postgres.py:703-709`, atomic insert-or-ignore race handling at
  `runtime/app.py:12327-12353`).
- **One execution path.** Both builders emit a `JobRecord`; the worker routes
  `JobKind.WORKFLOW` (`runtime/dispatch.py:119-120`) into `_execute_workflow`
  (`runtime/dispatch.py:866`), which resolves the workflow's declared runtime
  (ADR 055 fork, `runtime/dispatch.py:910-928`) and runs native or
  `run_temporal_workflow` (`runtime/dispatch.py:1011`,
  `runtime/workflow_backend.py:371`). Failures get the normal job retry /
  `DEAD_LETTER` lifecycle (`core/models.py:3247-3266`) and emit
  `RUN_COMPLETED`/`RUN_FAILED` lifecycle events (`runtime/worker.py:421-431`)
  that feed ADR 035 outbound webhooks. `POST /run` itself already supports an
  `Idempotency-Key` header (`runtime/app.py:4226`, `run_submissions` table).

So the gap is **not machinery — it is four residual surface gaps** that keep
the scenarios from working in practice:

1. **No clock-aligned schedules.** `cadence_seconds` expresses "every 6h", not
   "07:00 Mon–Fri" (the executive briefing). There is no cron-expression
   cadence (documented as a follow-up in `core/models.py:3429-3433`).
2. **No event→state mapping.** The fire endpoint merges the raw event body
   verbatim over `input_defaults` (`build_triggered_job`,
   `core/triggers.py:135-154`). An Azure DevOps work-item event is a deep
   envelope (`eventType`, `resource.fields["System.Title"]`, …) that would be
   splatted wholesale into the workflow's initial state — colliding with state
   keys and failing the state schema.
3. **Real senders can't sign our way.** The only fire auth is
   `X-Movate-Signature` HMAC keyed by `hash_secret(secret, salt)`. GitHub
   signs the same way but sends `X-Hub-Signature-256`; Azure DevOps Service
   Hooks **cannot compute a per-body HMAC at all** (they support basic auth /
   static headers only). And the dedup id only arrives via a header
   (`X-Movate-Delivery-Id`, `core/triggers.py:81`) — ADO carries its event id
   in the **body**, so ADO retries double-start today.
4. **No provenance or per-trigger delivery visibility.** A `JobRecord` started
   by a trigger or schedule is indistinguishable from a manual submit, and
   there is no endpoint listing a trigger's deliveries with the resulting job
   status.

## Decision

One ADR, two trigger kinds (cron + event), **zero new start paths**: every
decision below is an additive field or endpoint on the existing
`JobSchedule`/`Trigger` machinery, and every started run is still a normal
`JobRecord` flowing through `runtime/dispatch.py:119`.

### D1 — Schedule surface: keep `mdk schedule`, add cron expressions

`mdk schedule` (CLI + `/api/v1/schedules` + `job_schedules`,
`storage/postgres.py:655-668`) **stays the canonical scheduling surface** for
agents and workflows — we do not add an `mdk workflow schedule` twin or a
`schedules:` stanza in `workflow.yaml` (see Alternatives). What's added:

- An optional **`cron`** field (5-field cron expression) + optional
  **`timezone`** (IANA name, default UTC) on `JobSchedule`, the submission
  schema, and `mdk schedule set --cron "0 7 * * 1-5" --tz America/New_York`.
  Exactly one of `cron` | `cadence_seconds` per schedule (model validator).
- **Due semantics** extend `is_due` (`core/scheduler.py:83`): a cron schedule
  is due when the next occurrence after `last_enqueued_at` (or `created_at`
  for a never-fired schedule) is `<= now`. The tick fires it **at most once
  per tick** — a missed window (tick down for a weekend) yields ONE catch-up
  run, never a backfill storm. Precision is bounded by the external tick
  cadence (run the ACA Job every 1–5 min), same operational model as today.
- **Storage**: additive nullable `cron` + `timezone` columns on
  `job_schedules` (both backends); when `cron` is set, `cadence_seconds`
  persists as `0` (sentinel, relax the model bound to `ge=0` behind the
  exactly-one validator) so neither backend needs a NOT-NULL migration or a
  SQLite table rebuild.
- **Dependency**: cron-expression evaluation uses `cronsim` (MIT, zero-dep,
  single-module — passes `scripts/check_licenses.py`). Hand-rolling cron
  arithmetic (DST transitions, month-end wrap) is a known foot-gun and the
  opposite trade from ADR 094 D2: there the structured form *avoided* a
  parser; here the parser is the well-tested commodity and the hand-roll is
  the risk. Contestable — flagged in the review notes.

Example (executive briefing, scenario 7):

```
mdk schedule set exec-briefing -k workflow --cron "0 7 * * 1-5" \
    --tz America/New_York --input '{"audience": "leadership"}' \
    --notify-email ops@example.com
```

### D2 — Event surface: bind a trigger to a workflow with a declared input mapping + body-sourced dedup

Triggers already bind to `kind=workflow`; what's added is **how an event body
becomes the workflow's initial state** — deterministic, no LLM, mirroring the
ADR 094 D2 structured-over-DSL posture. Three optional `Trigger` fields (all
default `None` → today's verbatim-merge behavior, byte-for-byte):

- **`event_key: str | None`** — nest the raw event body under this single
  state key (e.g. `event`) instead of merging it at top level. The cheapest
  safe default for "give the workflow the whole payload" without state-key
  collisions.
- **`input_map: dict[str, str] | None`** — declared field extraction: output
  state key → dotted path into the event body, resolved with the same
  fail-soft semantics as the decision node's `_read_field`
  (`core/workflow/decision.py:62`) — a missing path means the key is
  **omitted** (the workflow's state schema then reports exactly what's
  missing), never an exception. No templates, no expressions, no eval.
- **`dedup_key: str | None`** — dotted path into the event body used as the
  delivery id when `X-Movate-Delivery-Id` is absent. This is what makes ADO
  replays safe: Service Hooks carry their event id at body path `id` and
  cannot send custom per-event headers. Resolved value is stringified and
  capped at the existing `DELIVERY_ID_MAX_LEN` (`core/triggers.py:82`);
  unresolvable → no dedup (today's behavior). Flows into the existing
  `trigger_deliveries` insert-or-ignore path unchanged
  (`runtime/app.py:12327-12353`).

Composition in `build_triggered_job` (deterministic, documented order):
`input = {**input_defaults, **mapped_fields, **({event_key: body} if event_key
else {})}`; when *neither* `event_key` nor `input_map` is set, the existing
verbatim merge `{**input_defaults, **body}` is preserved exactly.

ADO work-item triage (scenarios 23/27) example:

```
mdk trigger create work-item-triage -k workflow --name ado-work-items \
    --auth-mode token \
    --dedup-key id \
    --event-key event \
    --input-map '{"work_item_id": "resource.id", "event_type": "eventType"}'
```

Known limitation (flagged, not solved here): dotted-path segments cannot
address keys that themselves contain dots (ADO's
`resource.fields["System.Title"]`). The `event_key` verbatim capture covers
those — the workflow reads `event.resource.fields` itself — and an escape
syntax is deferred until a scenario actually needs mapped extraction of a
dotted key.

### D3 — Security: keep HMAC as the default; add a compat header alias + an opt-in static-token mode

What exists is already the right default and is unchanged: per-trigger
256-bit secret, hashed at rest exactly like an API key
(`core/triggers.py:100-132`), body-bound HMAC-SHA256 verification with a
constant-time compare (`core/triggers.py:179-196`), `admin`-scoped minting
(`runtime/app.py:12106`), plaintext shown once, and 404-without-existence-leak
on the fire path (`runtime/app.py:12282-12287`). The enqueued job is scoped to
the trigger's own tenant and can only ever start the trigger's bound
`target` — a leaked trigger secret's blast radius is one (tenant, target)
pair, never the API.

Two additive accommodations for real senders:

- **GitHub header alias.** Accept `X-Hub-Signature-256` as an alias for
  `X-Movate-Signature` (same `sha256=<hex>` HMAC semantics; the operator
  pastes the minted signing key into GitHub as the webhook secret). Zero new
  crypto; checked only when `X-Movate-Signature` is absent.
- **`auth_mode: "hmac" | "token"`** per trigger (default `"hmac"` — today's
  behavior). `token` mode verifies a static `X-Movate-Trigger-Token: <secret>`
  header by recomputing `hash_secret(token, salt)` and constant-time-comparing
  against the stored `secret_hash` — for senders that cannot HMAC (ADO Service
  Hooks' static-header support). Explicitly weaker: the secret travels on the
  wire (TLS-protected) and a captured request is replayable until rotation —
  so `mdk trigger create --auth-mode token` warns, and the docs require
  pairing it with `dedup_key` (D2) so a replayed capture can at worst re-run
  what already ran once. Contestable — see review notes.

### D4 — Failure + observability: fired-but-failed lands in the job lifecycle; add provenance + a deliveries view

Nothing new is needed for the failure path itself — that is the point of
converging on the job queue: a fired job that fails follows the normal
retry-then-`DEAD_LETTER` lifecycle (`core/models.py:3247-3266`), the
scaled-to-zero reaper backstop recovers orphans on every tick
(`core/scheduler.py:352-365`), and `RUN_COMPLETED`/`RUN_FAILED` lifecycle
events already fire for workflow jobs (`runtime/worker.py:421-431`), feeding
ADR 035 outbound webhooks and the ADR 095 business-of-record pane. Additions:

- **Job provenance.** An additive nullable `origin` field on `JobRecord` (+
  column on `jobs`): `{"source": "trigger"|"schedule", "name": <handle>,
  "delivery_id": <id?>}`, stamped by `build_scheduled_job` /
  `build_triggered_job` and absent for manual submits. Lets `mdk jobs`,
  `/api/v1/jobs`, and Grafana slice trigger-started vs scheduled vs manual
  runs, and lets an operator walk a dead-lettered job back to the trigger
  delivery that caused it. (Storage-schema change: additive nullable column,
  flagged per CLAUDE.md rule 5.)
- **Deliveries view.** `GET /api/v1/triggers/{name}/deliveries` (`read`
  scope, tenant-scoped): joins `trigger_deliveries` → `jobs` to list recent
  deliveries with `delivery_id`, `job_id`, job status, and timestamps. With D2
  `dedup_key`, ADO deliveries get rows here without sender cooperation.
  (`last_fired_at` on the trigger remains the cheap liveness signal,
  `core/models.py:3582-3586`.)

### D5 — Compatibility: purely additive

- `POST /run`, `RunSubmission` (`runtime/schemas.py:54-78`), and the worker
  dispatch are untouched — schedules and triggers keep producing the same
  `JobRecord` shape.
- All new model/storage fields are optional + nullable with defaults that
  reproduce today's behavior exactly; existing `job_schedules` / `triggers`
  rows keep working unread-and-unmodified.
- New CLI flags (`--cron`, `--tz`, `--event-key`, `--input-map`,
  `--dedup-key`, `--auth-mode`) and one new endpoint
  (`GET /triggers/{name}/deliveries`) are additive; no existing flag, `--json`
  shape, or `/api/v1` response changes.
- Explicitly flagged changes (per CLAUDE.md rule 5): additive nullable columns
  on `job_schedules` (`cron`, `timezone`), `triggers` (`event_key`,
  `input_map`, `dedup_key`, `auth_mode`), `jobs` (`origin`); one new shipped
  dependency (`cronsim`, MIT).

## Boundary (out of scope)

- **Queue-consumer triggers** (Service Bus / Event Grid / Kafka pull) — a
  different delivery model (movate polls) deserving its own ADR; the HTTP push
  surface covers the named scenarios.
- **Mid-workflow event waits** (an event resuming a paused run) — that is the
  HITL signal path (ADR 077/089), not a start trigger.
- **Cron backfill/catch-up policies** beyond fire-once-per-tick; revisit if a
  scenario needs "run every missed window".
- **Continuous eval** (scenario 12) already has its richer domain scheduler
  (`EvalSchedule`, `core/models.py:2684`, ADR 016 D2) — deliberately not
  re-homed here, exactly as the `JobSchedule` validator documents
  (`core/models.py:3487-3503`).

## Alternatives considered

- **Temporal native Schedules as the primary scheduler.** Temporal Schedules
  are genuinely richer at the scheduling layer (overlap policies, catch-up
  windows, pause/resume, jitter, first-class UI) — for an all-Temporal fleet
  they would be the better pure scheduler, and that case is argued honestly.
  Rejected as the *primary* because: (a) they only start Temporal workflows —
  `runtime: native`/`auto`-resolving-native workflows and **agent** schedules
  (which `JobSchedule` also serves) would need a second mechanism; (b) a
  Temporal-started run bypasses the job queue — no `JobRecord`, so no tenant
  accounting, no `notify_email`, no `Idempotency`/origin, no `RUN_*` lifecycle
  events, invisible to `/jobs` and the ADR 095 business-of-record — recreating
  the exact "second workflow-start path" this ADR exists to prevent; (c) the
  control plane would need Temporal connectivity to manage schedules, leaking
  the execution plane across the cli⊥runtime boundary. If interval+cron prove
  insufficient, Temporal Schedules can later become a *backend* behind the
  `JobSchedule` surface (the tick delegating to Temporal for temporal-runtime
  rows) without changing the operator contract.
- **External cron hitting `POST /run` (status quo workaround).** Works today —
  `Idempotency-Key` makes retries safe (`runtime/app.py:4226`, verified) — and
  remains supported per ADR 017 D3. Rejected as the product answer: every
  external scheduler holds an `mvt_*` API key (credential sprawl), there is no
  managed inventory (`mdk schedule list`), no enable/disable, no provenance.
- **Airflow (or another orchestrator) for scheduling.** Rejected by ADR 017
  D1; nothing has changed the calculus.
- **A `schedules:`/`triggers:` stanza in `workflow.yaml`.** Rejected: a
  schedule is a *deployment/tenant binding* (which tenant? which environment's
  cadence?), not part of the workflow *definition*; baking it into the
  published artifact (ADR 088 bundles) would make the same bundle mean
  different things per environment. `WorkflowSpec` stays `extra="forbid"`
  (`core/workflow/spec.py:609-646`). A project-level *deployment* manifest
  binding schedules/triggers at `mdk deploy` time is a plausible follow-up.
- **Template-expression or LLM-assisted event mapping.** Rejected for the
  ADR 094 D2 reasons: an expression DSL needs a parser/evaluator (security +
  determinism surface), an LLM is non-deterministic on a path that must be
  replay-exact. Dotted-path extraction + verbatim nesting covers the named
  scenarios with zero parsing.

## Consequences

- Scenarios 5/7/12/13/21/23/27 unblock on existing machinery: scheduled
  research/briefing/KB-refresh workflows via `mdk schedule set --cron`,
  incident-response and ADO work-item triage via a workflow-bound trigger with
  `input_map`/`dedup_key`/`auth_mode: token` — all starting runs through the
  one `POST /run`-shaped job path, observable and retryable for free.
- The ADO Service Hooks integration needs **no movate-side ADO code**: a
  service hook POSTs to the fire endpoint with a static token header; dedup
  rides the body event id.
- Operators get provenance (`origin`) and a per-trigger delivery ledger,
  closing the "what started this run / did my webhook fire" gap.
- Risks accepted: `token` auth mode is replayable-until-rotation (mitigated by
  dedup + per-trigger blast radius + an explicit CLI warning); cron precision
  is bounded by the external tick cadence (documented; same model as today);
  one new small MIT dependency (`cronsim`).
- Estimated scope: ~3 PRs — (1) cron cadence (model + tick + CLI/API), (2)
  trigger input mapping + body dedup + auth mode (model + fire endpoint +
  CLI/API), (3) provenance + deliveries view. Each independently additive and
  default-off.
