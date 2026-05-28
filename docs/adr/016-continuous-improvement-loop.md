# ADR 016 — The continuous-improvement loop: harvest → continuous eval → canary

**Status:** Accepted (partially shipped)
**Date:** 2026-05-23 (proposed); 2026-05-27 (accepted — D1/D2/D3 shipped)
**Deciders:** Engineering (eval/runtime/deploy-lifecycle — touches several seams)

> **Reconcile note (2026-05-27).** Accepted retroactively now the loop has
> shipped: harvest (`src/movate/core/harvest.py` + `POST /api/v1/agents/{name}/dataset/harvest`),
> continuous eval + drift alerting (`src/movate/core/drift.py`, the eval-schedule
> endpoints, and the drift check wired into `src/movate/runtime/dispatch.py`), and
> canary / champion-challenger with assisted promotion (`src/movate/core/canary.py`
> + the canary set/get endpoints in `src/movate/runtime/app.py`).
**Context window:** v1.0 — make the platform *compound* in value, not just ship once
**Builds on / depends on:** ADR 014 (durable agent registry — versions enable canary), ADR 015 (self-hosted observability — quality signals), ADR 013 (scopes — gate promotion), ADR 008 (workflow-level evals), ADR 001 (portability)
**Related (existing assets it composes):**
`StorageProvider.save_feedback`/`list_feedback` + `FeedbackRecord` + `POST /runs/{run_id}/feedback` (prod feedback capture — already shipped),
`JobKind.EVAL` + `_execute_eval` + `EvalRecord` + 4-dim scoring + baselines (eval-as-job — shipped),
the jobs/worker KEDA scaler, `NotificationDispatcher` + webhooks, `src/movate/promotions/store.py`

---

## Decision

Turn the lifecycle from **author → deploy** into **author → deploy → observe →
improve** by *connecting assets that already exist but don't talk to each
other*. Prod feedback is **captured** today (`POST /runs/{id}/feedback`,
`save_feedback`, pushed to the tracer) but **nothing consumes it**. Close the
loop with three composable capabilities:

1. **(D1) Harvest** — turn real prod runs (thumbs-down / low-score / sampled)
   into **proposed** eval-dataset cases, human-reviewed before they land. The
   test set grows from real usage instead of hand-authored guesses.
2. **(D2) Continuous eval + drift alerting** — run the existing eval suite on a
   **schedule** (and on every publish) against the live agent, diff vs. the
   stored baseline, and **alert** (Teams/email/webhook) on regression — catching
   prompt rot, model-version shifts, and KB/data drift.
3. **(D3) Canary / champion–challenger** — publish a challenger version (instant,
   via the ADR 014 registry), route a configurable **traffic %**, compare live
   quality (feedback + eval) champion-vs-challenger, and **assisted-promote** the
   winner (scope-gated, ADR 013).

In one sentence: **"prod feedback + eval + the agent registry become a loop —
harvest real runs into eval cases, catch quality drift on a schedule, and roll
out fixes safely via canary — so agents measurably improve over time instead of
silently rotting."**

---

## Context

Every ingredient for a feedback loop exists in isolation:
- **Capture:** `POST /runs/{run_id}/feedback` persists a `FeedbackRecord` and
  even pushes the score to the tracer (`push_run_feedback_score`).
- **Measure:** eval runs as a background job (`JobKind.EVAL`), scores 4
  dimensions, and compares to a baseline with regression detection.
- **Store/version (proposed):** ADR 014 makes agent versions durable; ADR 015
  keeps the quality/telemetry signal in-tenant.

But **none of it is wired into a loop.** Feedback is written and never read
back into improvement. Eval is a one-shot you run by hand, not a guardrail that
watches prod. New versions ship without a safe, measured rollout. The result:
an agent that's good on launch day silently degrades (a model provider updates
weights, the KB drifts, a prompt edit regresses an edge case) and **no signal
fires**. For an enterprise platform, the differentiator is not "can you ship an
agent" but "does it get *better* and not silently *worse*." This ADR is that
loop. It is mostly **wiring**, not new infrastructure — which is why it's
high-leverage now that 013/014/015 define the foundations.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Compounding value** — the platform should improve agents over time, and catch silent regressions | HIGH |
| **Reuse over new infra** — compose existing feedback + eval + registry + observability + jobs | HIGH |
| **Safety / failure modes** — feedback can be noisy/adversarial; promotion mustn't auto-ship a regression | HIGH |
| **Cost-awareness** — scheduled/canary eval spends LLM tokens; must be budgeted + sampled | MED |
| **Portability (ADR 001)** — the scheduler + alerts must be vendor-neutral | MED |
| **Least privilege (ADR 013)** — only authorized identities promote to prod | MED |

---

## Architecture

```
        ┌──────────────────── PROD ────────────────────┐
        │  runs + feedback (POST /runs/{id}/feedback)   │  ← captured TODAY
        │  traces / quality signals (ADR 015)           │
        └───────────────┬───────────────────────────────┘
                        │ (D1) harvest: thumbs-down / low-score / sampled
                        ▼            → PROPOSED eval cases (human-reviewed)
        ┌──────────────────── EVAL DATASET (in the bundle/registry, ADR 014) ──┐
        └───────────────┬───────────────────────────────────────────────────────┘
                        │ (D2) scheduler → eval-as-job (JobKind.EVAL) on a cadence
                        ▼            + on every publish; diff vs baseline
        ┌──────────────────── DRIFT GATE ──────────────────────────────────────┐
        │  regression vs baseline?  → alert (Teams / email / webhook)            │
        └───────────────┬───────────────────────────────────────────────────────┘
                        │ fix → publish a CHALLENGER version (ADR 014 registry)
                        ▼ (D3) canary: route X% traffic; compare champion vs challenger
        ┌──────────────────── PROMOTE (scope-gated, ADR 013) ───────────────────┐
        │  assisted (human approves) | auto on eval-gate → new champion          │
        └───────────────────────────────────────────────────────────────────────┘
                        └────────────────► back to PROD (loop)
```

Everything left of "publish a challenger" already exists or is proposed; the new
code is the **scheduler**, the **harvest** transform, **canary routing +
version-tagged runs**, and the **drift comparison/alert**.

---

## Decisions

### Decision 1 (D1): Harvest prod runs into *proposed* eval cases (human-gated)

Add a harvest path that turns selected prod runs into eval-dataset cases:
- **Selection:** thumbs-down (cases to fix), thumbs-up (golden/positive),
  low-eval-or-judge-score, or a random sample — driven by the `FeedbackRecord`
  + `RunRecord` already stored.
- **Transform:** a run's input → a dataset case; the feedback/label → the
  expected/grade signal; provenance (`source_run_id`) recorded.
- **Surface:** `mdk eval harvest <agent>` + an API endpoint; harvested cases are
  **proposed**, not auto-merged — a human reviews and accepts them into the
  agent's eval dataset (which lives in the bundle/registry, ADR 014).
- **Why human-gated:** prevents feedback-poisoning (noisy/adversarial
  thumbs-down) from silently corrupting the test set.

### Decision 2 (D2): Continuous / scheduled eval + drift alerting

A **scheduler** enqueues the existing eval-as-job on a cadence (cron-like) and
**on every publish**, against the live agent + its **durable baseline** (shared,
per ADR 014 — not a laptop-local `baseline.json`):
- **Drift = a measured regression** (mean or per-dimension score drop beyond a
  configured tolerance) vs. the baseline → fire an **alert** via the existing
  `NotificationDispatcher` (email), Teams card, or webhook (#90).
- **Complementary live signal:** a declining prod thumbs-up rate / rising
  thumbs-down rate (from `list_feedback`) is a cheap leading indicator that can
  *trigger* an off-cycle eval.
- **Cost-aware:** scheduled eval spends tokens — honor tenant budgets, support
  `--mock` for smoke cadence, and sampling (eval a subset between full runs).
- **Portable scheduler:** a cron-triggered job-enqueue (no cloud-specific dep);
  on Azure a Container Apps cron job / KEDA-cron enqueues, the existing worker
  executes — portable to any scheduler.

### Decision 3 (D3): Canary / champion–challenger rollout

With versioned agents (ADR 014):
- **Route a configurable % of prod traffic** to a challenger version (weighted /
  sticky-by-session); **tag each run + trace with the agent version** so
  feedback + eval can be sliced champion-vs-challenger.
- **Compare** live quality (feedback rates + an eval pass) between versions and
  surface the delta.
- **Promote** the winner — **assisted by default** (a human approves), with
  **opt-in auto-promote** behind an **eval-gate + the publish scope (ADR 013)**.
  Reuse `promotions/store.py` for the dev→staging→prod record.
- **Roll back** = promote the prior version (instant, via the registry).

### Decision 4 (D4): The loop is the product; each piece is independently useful

D1/D2/D3 each ship value alone (harvest grows the test set; continuous eval
catches drift; canary de-risks rollout), but wired together they form the
**flywheel**: prod → harvest → eval → catch regression → canary a fix → promote
→ prod. Ship them in that order; the loop emerges as they connect.

### Decision 5 (D5): Safety-first defaults (failure modes)

- Harvested cases are **proposed, never auto-applied** (anti-poisoning).
- Auto-promote is **opt-in** and **gated** (eval-gate + scope); the default is
  **assisted** (human approves). A drift alert **informs**; auto-rollback is
  opt-in, not default.
- Canary defaults to a **small %** with a kill-switch (route 0% instantly).
- All of it is **off by default** — an operator opts into the loop per agent/env.

### Decision 6 (D6): Reuse the seams; minimal net-new

Reuse: `FeedbackRecord`/`save_feedback` (capture), eval-as-job + `EvalRecord` +
baselines, the registry (ADR 014) for versions + datasets, observability (ADR
015) for signals, scopes (ADR 013) for promotion, the jobs/worker scaler for
scheduled runs, `NotificationDispatcher`/webhooks for alerts, `promotions/store`
for promotion records. **Net-new:** the scheduler, the harvest transform, canary
routing + version-tagged runs, and the drift comparator. No new external
dependency; the scheduler + routing are plain application logic.

---

## Consequences

**Positive**
- Agents **measurably improve** over time and **silent regressions get caught** — the core enterprise differentiator.
- The eval set grows from **real usage**, not guesses — better coverage with less hand-authoring.
- **Safe iteration:** canary + assisted-promote + instant rollback (via the registry) make shipping a change low-risk.
- High leverage: it's mostly **wiring existing assets**, gated by the 013/014/015 foundations.

**Negative / costs**
- New moving parts: a scheduler, canary traffic routing (stateful-ish), version-tagged runs, drift comparison — and the operational care they need.
- **Cost:** scheduled + canary eval spends LLM tokens — needs budgeting + sampling.
- **Depends on** ADR 014 (registry/versions) and 015 (signals) landing first; canary + durable baselines aren't meaningful without the registry.

**Neutral**
- New config (cadence, drift tolerance, canary %, auto-promote toggle) + new CLI/API surface (`mdk eval harvest`, canary/promote commands) — all additive, default-off.

---

## Implementation plan (separate PRs, after this ADR + its dependencies)

1. **(D1) Harvest** — `mdk eval harvest <agent>` + API; run+feedback → *proposed*
   eval cases with provenance; human-accept appends to the dataset. (Can land on
   today's eval/feedback assets; nicer once datasets live in the registry, ADR 014.)
2. **(D2) Continuous eval + drift** — durable shared baselines (with ADR 014);
   a portable scheduler enqueuing eval-jobs on cadence + on publish; baseline-diff
   drift detection; alerts via `NotificationDispatcher`/webhook. Budget + sampling aware.
3. **(D3) Canary** — version-tagged runs/traces (with ADR 015); weighted prod
   routing to a challenger; champion-vs-challenger comparison; assisted-promote
   (scope-gated, ADR 013) + instant rollback; `promotions/store` records.
4. **(D4) Wire the loop** — surface the flywheel in `mdk` + the UI: "harvested N
   cases", "drift detected", "challenger +0.04 — promote?".

## Risks / open questions

- **Feedback poisoning** — adversarial/noisy thumbs-down skewing harvested cases
  → the human-review gate (D1) + provenance + optional reviewer scope.
- **Eval cost** at cadence/canary scale → tenant-budget-aware, sampling, `--mock`
  smoke cadence; make the cadence configurable per criticality.
- **Canary routing statefulness** — start simple (random % or sticky-by-session
  header), avoid a heavy traffic-management layer; the edge gateway (ADR 013 L3)
  could do weighted routing later.
- **Auto-promotion trust** — default assisted; auto-promote only behind a strict
  eval-gate + explicit opt-in (a bad auto-promote ships a regression to all
  users — the exact failure this ADR exists to prevent).
- **Sequencing** — most value needs ADR 014 (versions/registry) first; D1
  (harvest) can start earlier against today's eval/feedback.
