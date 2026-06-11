# Human Escalation pattern — governance

Topology: `triage(answer + confidence) → DECISION(confidence ≥ 0.8) → {finalize | [HITL review routes approve/reject, feedback]} → finalize | rejected`

Low-confidence human escalation with resume-with-feedback, durable on
Temporal. The triage LLM agent drafts an answer AND self-scores a calibrated
numeric `confidence`; a deterministic `decision` node (ADR 094) routes
`confidence gte 0.8` straight to the finalize agent — **the model's
self-assessment is routed by a pure numeric predicate, not by a second LLM
judging the first**. Everything below the threshold pauses durably at the
`review` HUMAN gate (output_contract `[decision, feedback]`), which routes
its own structured `decision` (ADR 099): approve→finalize, reject→rejected,
prose fails safe to rejected. The reviewer's `feedback` merges into state on
signal and the finalize agent's prompt INCORPORATES it when present (Jinja
`is defined`) — the human's guidance shapes the final answer
(resume-with-feedback). Auto + approve paths **converge on one finalize**
and every reject lands on **one rejected node** (exclusive convergence,
ADR 098).

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Confidence-gated autonomy | the `confidence-check` `decision` node — the model only ships unreviewed when its own calibrated score clears the threshold. No model call in the routing itself. |
| Honest confidence calibration | the triage prompt pins the scale: 0.9+ ONLY for unambiguous single-fact questions, ≤0.4 for anything subjective/ambiguous/speculative, never in between — the threshold has teeth because the prompt forbids hedging into it. |
| Durable human-in-the-loop | the `review` HUMAN gate pauses durably (survives worker restarts) until a `POST /api/v1/workflow-runs/{id}/signal`. |
| Structured review contract | `output_contract: [decision, feedback]` — the signal endpoint 422s on a missing key, so every review carries both the verdict AND the guidance. |
| Resume-with-feedback | the finalize agent's prompt threads `{{ input.feedback }}` in when present (and says "none" on the auto path) — one agent serves both the reviewed and unreviewed paths. |
| Deterministic decision routing on the gate | `routes`/`fallback` on the HUMAN node (ADR 099) — trim+casefold exact match; anything else fails safe to `rejected`. |
| Self-contained agents | `triage` / `finalize` / `rejected` bundled under `agents/` with correct schemas + JSON-instructed prompts. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway revise loop is impossible by construction — feedback threads through ONE finalize pass, not an unbounded loop. |
| The threshold is deterministic | the `decision` node is a pure predicate over `state.confidence` | the autonomy boundary replays identically on Temporal; nobody can prompt-inject the router. |
| Review routing is deterministic | the gate's `routes`/`fallback` (ADR 099) | an out-of-vocabulary verdict can only land on the fail-safe `rejected` path. |
| Low confidence cannot self-approve | `finalize` is reachable below the threshold ONLY via the gate's approve route | every sub-threshold answer that ships was seen by a human. |
| No hidden side effects | the pattern has NO tool node — its outputs are answers, not mutations | pair with the purchase-order / approval-timeout patterns when an external write is the goal. |

## Customize

- Tune the threshold: edit the `decision` predicate (`gte 0.8`) to your risk
  appetite — and keep the triage calibration rules in sync (the prompt's
  "never between 0.4 and 0.9" dead zone should straddle your threshold).
- Tighten the contract: add keys to the review gate's `output_contract`
  (e.g. `category`) — the signal endpoint enforces them and they merge into
  state for the tail agents.
- Loop instead of single-pass: aim the gate's reject route at a revise agent
  feeding a SECOND bounded review (the reflective-agent template shows the
  bounded-loop shape) — never an unbounded cycle.
- Swap the tail: point `finalize`/`rejected` at your notification channel or
  ticketing system via a TOOL node (ADR 097).

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls on every path**
(triage, then finalize or rejected — the confidence decision and the gate
routing are deterministic, zero-cost). Cap absolute spend with the agent
`budget.max_cost_usd_per_run` field or a governance COST gate (ADR 093); the
eval-gate below is the quality budget.
