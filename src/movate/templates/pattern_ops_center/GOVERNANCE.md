# Ops Center pattern — governance

Topology: `TOOL fetch-facts → summarize → DECISION(failure_count) → {[HUMAN page ack→report, fallback report] | report}`

AI ops-center daily summary over the unified observability reporting surface
(ADR 096), durable on Temporal — **zero LLM calls on the facts pull and zero
LLM calls on the control path**. The entry `fetch-facts` **TOOL node
(ADR 097)** runs a workflow-local python skill returning canned,
replay-identical rows shaped exactly like `observability_facts` (the flat
columns `GET /api/v1/observability/facts` serves) and recording one auditable
`{system: observability, action: fetch_facts}` ledger row; the optional
`facts_endpoint` input documents the real endpoint, but the sim NEVER does
network IO — it echoes what it would have queried in `facts_source`. The ONE
summarizing LLM call writes totals/failures/top risks STRICTLY from the rows
under mechanical counting rules and emits a `failure_count`; the
deterministic `decision` node (ADR 094) pages the on-call at a HUMAN gate
(ADR 099) when failures exist — **fail-open by design**: `ack` and any other
wording both proceed to the report (a page can delay the daily report, never
kill it) — and a clean window reports directly. Both paths converge on ONE
report agent (ADR 098) whose prompt guards the path-exclusive `decision` key
(`| default("n/a")` — the Jinja StrictUndefined rule).

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Reporting on the ADR 096 surface | the fact rows mirror `observability_facts` column-for-column — the summary reads what the platform reads, never a bespoke export. |
| Deterministic skill execution as a step | the `fetch-facts` TOOL node (ADR 097) — one `dispatch_skill` call, schema-validated in and out, SKILL-gate governed, no LLM. |
| Grounded summarization | the `summarize` agent's input schema admits ONLY the window + the fetched rows; its counting rules are mechanical. |
| Deterministic value routing | the `gate` `decision` node — `failure_count > 0` → page, else → report. No model call. |
| Durable human-in-the-loop | the `page` HUMAN node pauses durably (survives worker restarts) until a `POST /api/v1/workflow-runs/{id}/signal`. |
| Fail-open gate routing | `routes`/`fallback` on the HUMAN node (ADR 099) — `ack` routes to report and the FALLBACK is also report: silence about wording can delay, never kill, the daily report. |
| Path-exclusive state, guarded | `decision` exists only on the paged path; the report prompt reads it via `| default("n/a")`. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway loop is impossible by construction. |
| The facts pull is deterministic | the TOOL node returns canned rows and never does network IO | the pull replays identically on Temporal — and going live swaps the impl body, not the topology. |
| Paging is deterministic | the `decision` node is a pure predicate over `state.failure_count` | the page/no-page decision replays identically — no model in the control path. |
| The report always lands | the HUMAN gate's only route AND its fallback both target `report` | the on-call can acknowledge or mistype; neither can prevent the daily report. |
| The pull is governed | the skill declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) statically and at runtime | the external read cannot hide behind a prompt. |

## Customize

- Point `sim-fetch-facts` at your real runtime: swap the impl body for an
  authenticated GET against `facts_endpoint`
  (`GET /api/v1/observability/facts`), keep the schema contract.
- Make the page blocking: change the gate's `fallback` (and add a `reject`
  route) if your policy says an unacknowledged failure must NOT report —
  the fail-open default is this pattern's deliberate posture, not a law.
- Tune the failure definition in `agents/summarize/prompt.md` (status !=
  "success" today) — or harden the counting into a deterministic skill if
  the threshold must be provable.
- Schedule it: the same ADR 100 cron binding as the executive-briefing
  pattern (`mdk schedule set ops-center -k workflow --cron "0 8 * * *" ...`)
  turns this into the daily ops digest.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls on every path**
(summarize, then report — the fetch TOOL node, the paging decision, and the
page gate's routing are all deterministic, zero-cost). Cap absolute spend
with the agent `budget.max_cost_usd_per_run` field or a governance COST gate
(ADR 093); the eval-gate below is the quality budget.
