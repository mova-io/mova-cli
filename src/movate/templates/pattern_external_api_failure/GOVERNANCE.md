# External API Failure pattern — governance

Topology: `TOOL flaky-call (retries ⟳3) → DECISION(provider_ok) → shared TOOL record → notify | failed`

Retries + fallback provider for an external-API integration, durable on
Temporal — **zero LLM calls on the control path and zero LLM calls on either
side-effecting step**. The entrypoint is a TOOL node (ADR 097) whose skill
makes the activity retry policy *auditable*: every invocation records one
`{system: external-api, action: attempt}` ledger row before deciding its
fate, and raises while its per-run attempt count is at or below the input's
`fail_times` — so ledger rows = activity attempts under the compiled
`_RETRY_POLICY` (`maximum_attempts=3`, ADR 054 D9). A transient failure
(`fail_times: 1`) is absorbed by the durable retry and served by the
fallback provider; an unrecoverable failure (`fail_times >= 3`) exhausts the
budget, fails the workflow loudly, and provably leaves NO downstream record
row. On success a deterministic `decision` node (ADR 094) routes
`provider_ok` into the shared record→notify tail.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Observable activity retries | the `flaky-call` TOOL node — its attempt rows in the sim ledger ARE the Temporal attempts; no internal trace needed. |
| Bounded retry budget | the compiled `_RETRY_POLICY` (`maximum_attempts=3`) — the third failed attempt errors the workflow rather than retrying forever. |
| Fallback-provider failover | the skill reports which provider served (`primary` on attempt 1, `fallback` after a retry); the notify agent surfaces it. |
| Loud terminal failure | retry exhaustion lands a terminal ERROR fact (ADR 096) — never a silent partial success, and never a downstream record row. |
| Deterministic decision routing | the `provider-check` `decision` node (ADR 094) — a pure `truthy` predicate over `provider_ok`; the `failed` default is the structural fail-safe for a malformed skill result. |
| Deterministic skill execution as a step | both TOOL nodes (ADR 097) — one `dispatch_skill` call each, schema-validated in and out, SKILL-gate governed, no LLM. |
| Self-contained workflow-local skills | `skills/flaky-call/` + `skills/sim-record/` carry their own `impl.py` next to `skill.yaml` — bake into a worker image with the workflow, no external package. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| Retries are capped | `_RETRY_POLICY (maximum_attempts=3)` on every activity | a permanently-down provider costs exactly 3 attempts, then fails loud. |
| The retry is auditable | one attempt row per invocation, written BEFORE the failure decision | operators read the retry history out of the audit ledger, not out of Temporal internals. |
| Failure gates the side effect | `record` is reachable only through the decision's success route, and the decision only runs after a successful call | an exhausted call provably leaves zero record rows. |
| The record write is governed | both skills declare `side_effects: mutates-state` and clear the SKILL gate (ADR 093/097) statically and at runtime | the external writes cannot hide behind a prompt. |

## Customize

- Point `flaky-call`'s impl at your real API client: keep the
  attempt-row + raise contract and you keep the retry observability.
- Point `sim-record` at your real downstream store (DB, queue, SaaS):
  swap the impl.py body, keep the schema contract.
- Tune the failure budget by adjusting the retry policy when ADR 054
  Phase 3 lifts it into workflow.yaml; today it is the compiled default
  (3 attempts).
- Aim the decision's `failed` default at a remediation/alerting agent
  instead of the plain notifier if a malformed result should page someone.

## Budget

Per-run LLM spend is bounded: **exactly 1 model call on every path** (notify
on the success tail, failed on the fail-safe default — the flaky call, the
decision, and the record step are deterministic, zero-cost; a retry re-runs
only the zero-cost skill activity, never an LLM call). Cap absolute spend
with the agent `budget.max_cost_usd_per_run` field or a governance COST gate
(ADR 093); the eval-gate below is the quality budget.
