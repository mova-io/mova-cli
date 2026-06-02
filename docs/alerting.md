# Alerting — route telemetry signals to Teams / Slack / email

`mdk` already *detects* the conditions you care about (drift regressions,
dead-letter spikes, budget thresholds). This guide turns that into something
that **acts**: routing those signals to notification sinks. It's **opt-in** —
with no routes configured, nothing fires (zero behavior change). Design: ADR 057.

## How it works

```
 drift / dead-letter / budget detector
        │  emits an AlertEvent (at the edge, into the ADR-035 events outbox)
        ▼
 AlertWorker  ──drains the outbox──▶  AlertRouter  ──matches routes──▶  sinks
 (runs inside `mdk worker`)                                    (Slack / Teams / webhook / email)
```

Sources and sinks never know about each other — you wire them with a **route
table**. The worker only routes alerts raised *after* it starts (its cursor is
in-process today; a durable cursor is a tracked follow-up).

## Step 1 — configure sinks (environment)

Each sink registers automatically when its env var is present (these ride the
same BYOK/credentials autoload as provider keys):

| Env var | Sink name | Notes |
|---|---|---|
| `SLACK_WEBHOOK_URL` | `slack` | Slack Incoming Webhook URL |
| `TEAMS_WEBHOOK_URL` | `teams` | Teams Incoming Webhook URL |
| `MDK_ALERT_WEBHOOK_URL` (+ optional `MDK_ALERT_WEBHOOK_SECRET`) | `webhook` | Generic HMAC-signed POST |
| `MDK_ALERT_EMAIL` | `email` | Recipient; uses the existing SMTP config (`MOVATE_SMTP_*`) |

## Step 2 — define routes (`alerts.yaml`)

Put an `alerts.yaml` at the project root (the whole file is the `alerts:` block),
**or** an `alerts:` block inside `movate.yaml` / `project.yaml`. Each route is a
`match` (an AND of any of `kind` / `min_severity` / `tenant` / `subject_glob`) →
a `sink`. First-match by default; the first route whose `match` is satisfied
wins, so order from most-specific to catch-all.

```yaml
# alerts.yaml
routes:
  # Critical model drift on any tenant → page the on-call Teams channel
  - match: { kind: drift_regression, min_severity: critical }
    sink: teams

  # Any dead-letter spike → the ops Slack
  - match: { kind: dead_letter_spike }
    sink: slack

  # A specific customer's budget warnings → their webhook
  - match: { tenant: acme, kind: budget_threshold, min_severity: warning }
    sink: webhook

  # Catch-all → email digest
  - match: {}
    sink: email
```

**Alert kinds:** `drift_regression`, `dead_letter_spike`, `budget_threshold`
(more added additively). **Severities** (ordered): `info` < `warning` <
`critical` — `min_severity` gates with `>=`. **`subject_glob`** matches the
alert's subject (the agent / job / tenant it's about) with shell-glob syntax.

## Step 3 — run

The router runs inside `mdk worker` (alongside the job + webhook workers). With
no `alerts:` routes **and** no sink env vars, it's a silent no-op. Add a route +
the matching sink env var and alerts start flowing on the next worker start.

## Behavior notes

- **Opt-in:** unconfigured ⇒ nothing read, nothing sent.
- **Best-effort:** a sink that errors or times out is logged and dropped — an
  alert delivery failure **never** breaks the detector that raised it.
- **No storms:** per-`(route, dedup_key)` throttling collapses a flapping signal
  into one delivery per window (suppressed count surfaced on the next send).
- **Complements Azure Monitor:** the Azure-native SLO/metric alerts (the
  Workbooks + alert rules) cover infra signals; this app-level router covers the
  *semantic* signals (drift, budget, eval-gate) and works on any cloud.

See [ADR 057](adr/057-alert-routing.md) for the full design, and
`src/movate/core/alert_sinks.py` to add a new sink.
