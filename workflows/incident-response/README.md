# incident-response — wiring a real alert source (ADR 100 triggers)

This workflow is event-shaped on purpose: its input
(`{alert: {service, severity, message}}`) is exactly what a webhook trigger
created with `--event-key alert` feeds it. Nothing below is required to run
the workflow (the certification suite drives it via `POST /run`, the same
JobRecord path a fired trigger enqueues) — this is the binding for a real
alert source (Alertmanager, PagerDuty, Datadog, ...).

## Register the trigger

Local CLI (persists to the storage your `MOVATE_DB`/`MOVATE_DB_URL` points
at — point it at the deployed Postgres to register against the deployed
runtime):

```bash
mdk trigger create incident-response -k workflow --name alertmanager-alerts \
    --auth-mode token --dedup-key id --event-key alert
```

Or over the deployed runtime API directly (admin scope — it mints a
long-lived secret):

```bash
curl -X POST "$RUNTIME_URL/api/v1/triggers" \
    -H "Authorization: Bearer $MDK_ADMIN_KEY" \
    -H "Content-Type: application/json" \
    -d '{"target": "incident-response", "kind": "workflow",
         "name": "alertmanager-alerts", "auth_mode": "token",
         "dedup_key": "id", "event_key": "alert"}'
```

Both print the webhook path (`POST /api/v1/triggers/{trigger_id}/events`)
and the per-trigger secret **once**.

## Why these flags

* `--event-key alert` (ADR 100 D2) — the raw event body nests under the
  single `alert` state key instead of merging at top level, so an alert
  source that POSTs `{"id": "...", "service": "...", "severity": "...",
  "message": "..."}` lands in workflow state as exactly the
  `{alert: {...}}` shape the `diagnose` agent's input schema expects. No
  per-source mapping glue; extra body fields ride along harmlessly
  (`additionalProperties: true`).
* `--dedup-key id` (ADR 100 D2) — a dotted path into the **event body**
  (pre-nesting): when the source omits the `X-Movate-Delivery-Id` header,
  the body's `id` becomes the delivery id, so at-least-once webhook
  redeliveries return the SAME job instead of re-running the incident.
* `--auth-mode token` (ADR 100 D3) — most alert sources can set a static
  header (`X-Movate-Trigger-Token`) but cannot compute an HMAC over the
  body. Token mode is explicitly the weaker choice (the secret travels on
  the wire; a captured request replays until rotation) — which is WHY it is
  paired with `--dedup-key`: a replay can at worst re-run what already ran
  once. Sources that can sign (GitHub-style `X-Hub-Signature-256` is
  accepted as an alias) should drop `--auth-mode token` and use the default
  HMAC mode instead.

## Fire it

```bash
curl -X POST "$RUNTIME_URL/api/v1/triggers/$TRIGGER_ID/events" \
    -H "X-Movate-Trigger-Token: $TRIGGER_SECRET" \
    -H "Content-Type: application/json" \
    -d '{"id": "ALRT-2026-0610-001", "service": "payments-api",
         "severity": "high", "message": "Connection pool exhausted ..."}'
```

The enqueued job is the same shape `mdk submit` / `POST /run` produce, so
the run is observable and retryable like any other — and shows up on the
ADR 096 facts surface with the workflow's certification asserts intact.
`tests/test_b5_scenarios.py` smoke-tests this registration (real CLI) and
the event→state mapping (`build_triggered_job` + `resolve_body_delivery_id`)
so the commands above cannot drift from the implementation silently.
