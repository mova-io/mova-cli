# ADR 057 — Alert routing: telemetry signals → notification sinks (one seam, opt-in)

**Status:** Proposed
**Date:** 2026-05-30
**Deciders:** Engineering (observability/runtime)
**Context window:** turn the observability we already emit (drift, dead-letter,
budget, SLO) into something that *acts* — routes typed alert events to
notification sinks (email/Slack/Teams/PagerDuty/webhook) through one seam,
without each signal hardcoding its own delivery.
**Builds on / composes with (changes nothing in any of them):**
ADR 035 (events outbox — the durable event stream alert sources emit into),
ADR 036 (per-tenant budgets — a budget-threshold alert source),
ADR 016 (improvement loop — drift/regression is an alert source),
the **`NotificationDispatcher` Protocol** (`core/notify.py` — email/SMS/console;
this ADR extends it with new sinks, it does not replace it),
the job retry / dead-letter machinery (`core/job_retry.py` — dead-letter-spike
is an alert source), and the **golden-signal SLOs** (tracker #27 — SLO-breach
becomes an alert source once that lands).

**Defining fact.** mdk already *detects* the conditions operators care about —
drift regressions, dead-letter accumulation, budget burn, (soon) SLO breaches —
and it already has a `NotificationDispatcher` that can send email/SMS. What it
lacks is the **wire between them**: a way to say "a critical drift on tenant X
goes to PagerDuty; a dead-letter spike goes to the ops Slack; everything else is
email." Today each signal would have to know about each sink (an N×M mess), and
most signals notify nothing at all. This ADR adds **one routing seam** so signals
emit *typed alert events* and a router delivers them to *configured sinks* —
sources and sinks never know about each other.

This is a **design** ADR. It is **additive and opt-in**: with no routes
configured, nothing changes (no alerts fire) — exactly today's behavior. The
router, the route config, and the new sinks land in follow-up PRs.

---

## Context

Three forces converge:

1. **Detection exists; delivery doesn't.** Drift (ADR 016), dead-letter
   (`job_retry`), and budget (ADR 036) conditions are computed today but mostly
   just logged or persisted. An operator finds out by *looking*. The value of a
   dashboard that *pages you* (vs one you stare at) is the gap between "we have
   metrics" and "we have operations."
2. **Hardcoding delivery per signal is an N×M trap.** If the drift detector
   imports the Slack client and the dead-letter reaper imports PagerDuty, every
   new signal × every new sink is bespoke wiring, and the execution-plane code
   grows knowledge of notification backends (a boundary violation — alerting
   belongs at the edges, ADR 035 / CLAUDE.md rule 6).
3. **We already have the two halves.** The events outbox (ADR 035) is the
   natural carrier for alert events; the `NotificationDispatcher` Protocol is the
   natural sink abstraction. This ADR is *connective tissue*, not new machinery —
   it adopts the existing seams rather than inventing a framework (rule 8).

## Decision

Introduce a single **alert-routing seam**: sources emit typed `AlertEvent`s; a
configurable `AlertRouter` matches each event to one or more notification sinks
and delivers it best-effort. Sources and sinks are fully decoupled.

### D1 — `AlertEvent` (typed, source-agnostic)

A small typed event every alert source emits:

```
AlertEvent(
  kind:      AlertKind,        # drift_regression | dead_letter_spike |
                              # budget_threshold | slo_breach | job_failure_rate | ...
  severity:  Severity,         # info | warning | critical
  tenant_id: str,
  subject:   str,              # the agent / job / tenant the alert is about
  summary:   str,              # one-line, human-readable (what + how bad)
  data:      dict,             # structured context (scores, thresholds, ids, trace_id)
  dedup_key: str,              # stable key for throttle/dedup (D5)
)
```

`AlertKind` is a `StrEnum` that grows additively. Sources emit `AlertEvent`s
into the **ADR 035 events outbox** (alerting is wired at the edge — the drift
detector / dead-letter reaper / budget checker emit; they never import a sink).

### D2 — `AlertRouter` over configurable routes

The router consumes `AlertEvent`s and resolves each to zero+ sinks via an
ordered **route table**:

```yaml
# alerts.yaml (or a movate.yaml `alerts:` block)
routes:
  - match: { kind: drift_regression, min_severity: critical }
    sink: pagerduty-oncall
  - match: { kind: dead_letter_spike }
    sink: ops-slack
  - match: { tenant: acme, min_severity: warning }
    sink: acme-webhook
  - match: {}                       # catch-all
    sink: ops-email
```

A `match` is an AND of `{kind?, min_severity?, tenant?, subject_glob?}`; the
first matching route (or all matching, configurable) selects the sink. No routes
configured ⇒ no delivery (opt-in, D7).

### D3 — Sinks extend the existing `NotificationDispatcher` Protocol

New sinks are **new backends behind the existing `core/notify.py` Protocol**
(adapter pattern, ADR 017 "adapt — don't adopt"): `SlackSink`, `TeamsSink`,
`PagerDutySink`, `GenericWebhookSink` (HMAC-signed POST), alongside the existing
`SmtpEmailBackend` / console. No new notification framework; each sink is a thin,
permissively-licensed (or stdlib-`urllib`) HTTP adapter. Sink credentials ride
the **same BYOK seam as everything else** (ADR 018 — `SLACK_WEBHOOK_URL`,
`PAGERDUTY_ROUTING_KEY`, … from the credentials autoload).

### D4 — Severity + throttle + dedup (no alert storms)

A flapping signal must not page someone 400 times (failure-mode rule):

- **Severity** (`info`/`warning`/`critical`) is set by the source and gates
  routes (`min_severity`).
- **Throttle:** per (route, `dedup_key`) window — at most one delivery per
  window (default e.g. 15 min, configurable). Suppressed duplicates increment a
  count surfaced in the next delivery ("+37 since 12:04").
- **Dedup_key** (D1) makes "the same alert" identifiable across repeats.

### D5 — Delivery is best-effort and never blocks the source

Alerting must never break execution or the loop that emitted it (rule 10):
delivery is async/best-effort with bounded retry; a sink that errors or times
out **logs and is dropped**, never raised back into the drift detector / worker /
budget checker. An optional **delivery log** (an `alert_deliveries` row, additive)
records sent/suppressed/failed for audit — opt-in.

### D6 — Relationship to Azure Monitor alert rules (#27)

The golden-signal SLO work (tracker #27) configures **Azure Monitor alert rules**
at the *infra* layer (latency/error/queue-depth → Azure-native alerts). This ADR
is the **portable, app-level** router that works on any cloud / local / on-prem
and covers *semantic* alerts Azure Monitor can't see (drift regression, budget
burn, eval-gate failures). They **complement**: #27 is infra-side and
Azure-specific; this is app-side and portable. An `slo_breach` `AlertEvent`
(emitted when the app evaluates an SLO) flows through this router too, so SLO
alerting works even off-Azure.

## Consequences

**Positive**
- The conditions we already detect become **actionable** — drift/dead-letter/
  budget/SLO can page, Slack, or webhook, configurably, on any deployment.
- **One seam**, N sources × M sinks decoupled — adding a sink or a source is a
  thin adapter / one emit call, never bespoke N×M wiring.
- Execution-plane stays clean (sources emit events at the edge; the router and
  sinks live at the edge); reuses ADR 035 outbox + the dispatcher Protocol.

**Negative / risks**
- **Alert storms** — mitigated by D4 throttle/dedup; the default config is
  conservative.
- **Sink reliability / secrets** — each external sink is a failure + a credential
  surface; D5 makes delivery non-blocking + best-effort, BYOK (ADR 018) holds the
  secrets, HMAC signs outbound webhooks.
- **Another config file** — `alerts.yaml` is opt-in; absent ⇒ no behavior change.

## Boundaries

Alerting is wired at the **edges** — sources emit `AlertEvent`s into the ADR 035
outbox; the router + sinks consume there; **nothing in execution logic imports a
sink** (rule 6). `core` depends on the `NotificationDispatcher` Protocol; sinks
are adapters (rule 7). Additive, opt-in, behavior-preserving default; sink creds
via the existing BYOK autoload (no new credential model). Adapt — don't adopt
(no alerting framework).

## Alternatives considered

- **Hardcode notifications per signal.** Rejected — N×M wiring + execution-plane
  code growing knowledge of Slack/PagerDuty (boundary violation).
- **Azure Monitor alert rules only.** Rejected as the *only* path — infra-side,
  Azure-specific, and blind to semantic signals (drift/budget/eval-gate). Kept as
  the complementary infra layer (#27 / D6).
- **Adopt a full alerting framework (Alertmanager, etc.).** Rejected — heavyweight
  dep + a second config/runtime to operate; the router is ~a match table + thin
  HTTP sinks (rule 8). Alertmanager remains a possible *sink* (webhook) for teams
  that run it.
- **Per-tenant inline callbacks.** Rejected — not portable, not auditable, easy to
  turn into an alert storm.

## Scope / rollout

Multi-PR; this ADR is doc-only.

1. **The seam** — `AlertEvent` + `AlertKind`/`Severity` + `AlertRouter` + route
   config loader + extend `NotificationDispatcher` with `SlackSink` /
   `TeamsSink` / `GenericWebhookSink` (+ throttle/dedup, D4) + opt-in delivery
   log. Ships value the moment one route is configured.
2. **Wire existing sources** — drift (ADR 016), dead-letter (`job_retry`), budget
   (ADR 036) emit `AlertEvent`s into the outbox.
3. **`slo_breach`** source — once tracker #27 (golden-signal SLOs) lands.
4. **PagerDuty sink + dedup/throttle hardening + delivery-log dashboard panel.**
