# ADR 031 — Reporting & dashboarding: surface the telemetry, don't rebuild it

**Status:** Accepted
**Date:** 2026-05-27 (proposed); 2026-05-27 (approved)
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x operability — turn the already-rich telemetry into
reporting + dashboards the team can actually look at.
**Builds on / related:** ADR 024 (per-step spans + cost), ADR 015 (Langfuse v3,
OTLP → Azure Monitor), ADR 016 (continuous eval + drift), item 27 (golden-signal
SLOs + Azure Monitor alerts), `src/movate/tracing/` (langfuse / otel / metrics /
audit sinks via the composite Tracer), `mdk runs` / `explain` / `trace`.

## Context

The telemetry already exists and flows: traces + per-step cost (ADR 024),
Langfuse + OTel + Azure Monitor sinks (ADR 015, via a composite `Tracer`), eval
results + drift (ADR 016), SLO alerts (item 27), control-plane audit. The gap is
**presentation + aggregation**, not collection. The best practice for an
embedded, portable, minimal-deps product is to **surface this through
best-in-class OSS** (Langfuse for LLM observability, Grafana/Azure for ops) and a
lightweight offline CLI — **not** to build and maintain a bespoke web dashboard
(the framework-sprawl trap, CLAUDE.md rule 8).

## Decision

Ship reporting across **three complementary surfaces**, none of which adds a core
dependency:

### D1 — Deepen Langfuse (the primary LLM-observability dashboard)
Langfuse is already integrated (v3). Make it the answer for "how are my agents
doing":
- Push **eval results + drift as Langfuse scores** (per run / per dimension), so
  pass-rate, drift, and quality trends render in Langfuse natively.
- **Sync eval datasets** to Langfuse datasets (harvested cases, ADR 016 D1).
- **Link every `mdk run` / `mdk trace` to its Langfuse trace URL** (surface in
  `mdk runs`/`explain`/`trace` output + deploy next-steps), so jumping from CLI
  to the dashboard is one click.

### D2 — Grafana + Azure dashboards as code
Over the **existing OTel metrics** (ADR 015 / item 33 / item 27), ship in-repo,
versioned dashboards customers import — no bespoke server:
- **Grafana dashboards + Prometheus alert rules** (JSON/yaml in a `dashboards/`
  dir) for golden signals: latency p50/p95/p99, error rate, throughput, queue
  depth, cost.
- An **Azure Monitor workbook** (for the Azure-native deployment) over the same
  OTLP → Azure Monitor stream + the SLO alerts.
- A short runbook for importing them.

### D3 — `mdk report` (offline CLI rollup)
A CLI aggregation from the local store for the no-infra / offline case
(extends `mdk runs`/`explain`/`trace`): pass-rate **trends**, **cost over time**,
**latency percentiles**, **top failing eval cases**, per-agent / per-workflow
rollups. `--json` for scripting. The zero-dependency, works-on-a-laptop answer.

## Consequences

**Positive**
- Reporting on day one via tools that already do it best (Langfuse, Grafana,
  Azure) — minimal build, minimal maintenance, no core dep.
- Three surfaces cover the spectrum: rich LLM observability (Langfuse), ops
  dashboards (Grafana/Azure), and offline/no-infra (`mdk report`).
- Dashboards-as-code are reviewable + versioned + portable.

**Negative / risks**
- **Langfuse OSS vs. EE feature boundary** — rely on OSS/MIT-core features;
  note any EE-only capability rather than depending on it.
- **Dashboard ↔ metric-name drift** — pin metric/attribute names; a test
  asserts the dashboards reference metrics that actually exist.
- **`mdk report` scope creep** — keep it aggregation/rollup, not a viz engine;
  rendering lives in Langfuse/Grafana.

## Boundaries
No new core dependency (Grafana/Prometheus/Azure are external infra the customer
runs; the dashboards are config; Langfuse is already integrated). `cli ⊥
runtime`; tracing stays wired at the edges. `mdk report` reads the local store
through existing read paths.

## Alternatives considered
- **A bespoke mdk web dashboard.** Rejected — framework sprawl + ongoing
  maintenance when Langfuse + Grafana already do it better.
- **Only `mdk report` (CLI-only).** Rejected — insufficient for team/ops
  visibility; Langfuse + Grafana are where teams already look.

## Scope / rollout
Three independent PRs (any order): D3 (`mdk report`) is quickest + most portable;
D1 (Langfuse deepening) is highest leverage; D2 (dashboards-as-code) is
ops-facing. No dependency gate (additive over existing sinks).
