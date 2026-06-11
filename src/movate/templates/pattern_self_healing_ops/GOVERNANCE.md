# Self-Healing Ops pattern — governance

Topology: `TOOL detect → triage → TOOL remediate-1 → DECISION(r1_status) → {closure | TOOL remediate-2 → DECISION(r2_status) → {closure | [HUMAN ack] → closure}}`

Infrastructure self-healing with two-attempt remediation, durable on
Temporal — **remediation is bounded and every attempt is ledgered**. The
entry `detect` TOOL node (ADR 097) maps the monitor's signal onto the canned
fault catalog deterministically (one `{system: monitor, action: detect}`
ledger row); ONE `triage` agent makes the workflow's single judgment call
(enum-pinned severity + the recommended remediation action). The action is
then APPLIED deterministically, **twice at most**: mdk workflows have no
cycles, so "retry on failure" is UNROLLED in the graph as two sequential
TOOL attempts (`sim-remediate-ops`, then `sim-remediate-retry`), each verified
by a pure `decision` node (ADR 094) and each writing its own `{system: ops,
action: remediate}` row (attempt 1 / attempt 2). A fault that survives both
attempts ("hardware") pauses durably at the `escalate` HUMAN gate (ADR 099 —
operator `ack`; any other wording falls back the same way). All three exits
converge on ONE shared `closure` agent (exclusive convergence, ADR 098)
whose prompt guards the keys only some paths set.

## What this pattern demonstrates

| Capability | Where |
|---|---|
| Bounded retry WITHOUT a loop | the unrolled `remediate-1` → `remediate-2` chain — the attempt cap (2) is structural, wired in workflow.yaml, not a runtime suggestion. |
| Deterministic detection | the `detect` TOOL node (ADR 097) — the fault that drives the run is a closed canned catalog (loud KeyError outside it), not a model's reading of the signal. |
| Triage without authority | the `triage` agent recommends; `sim-remediate-ops`'s outcome is a pure predicate over the FAULT — the LLM cannot talk an attempt into "applied". |
| Deterministic verification routing | the `verify-1` / `verify-2` `decision` nodes (ADR 094) — pure predicates over each attempt's status; no LLM in the control path. |
| Auditable attempts | one `{system: ops, action: remediate}` ledger row PER attempt (attempt 1 / attempt 2 in the payload) — a `times: 2` count proves the retry ran. |
| Durable human-in-the-loop on exhaustion | the `escalate` HUMAN gate (ADR 099) pauses durably after the second failure; the operator's `ack` closes the run. |
| Shared closure with guarded context | ONE `closure` agent for all three exits (ADR 098) — its prompt guards `r2_status`/`decision` (`| default("n/a")`), the keys only some paths set. |

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| No cycles | the compiler rejects cyclic graphs at compile time | a runaway remediation loop is impossible by construction — the retry is a second node, not a back-edge. |
| The attempt cap is structural | exactly TWO remediation TOOL nodes in the graph | a third attempt cannot happen without an explicit workflow change. |
| Attempt outcomes are deterministic | `sim-remediate-ops` / `sim-remediate-retry` are pure predicates over the fault (transient faults need the retry; hardware never applies) | the verification decisions replay identically on Temporal. |
| Exhaustion cannot self-close | `escalate` is reachable only after BOTH attempts failed, and the escalated run still ends at the SAME closure (with the operator's ack in state) | the graph guarantees a human sees every fault automation could not fix. |
| Prose answers fail safe | the gate's `fallback: closure` (ADR 099) | however the operator words the acknowledgment, the run can only land on the shared closure. |
| Remediation is governed | every skill declares `side_effects: mutates-state` and clears the SKILL gate (ADR 093/097) | the infrastructure write cannot hide behind a prompt. |

## Customize

- Point `sim-detect` at your real monitor/alerting (the signal → fault
  mapping): swap the impl.py body, keep the `fault` + `component` contract.
- Point `sim-remediate-ops` / `sim-remediate-retry` at your real runbook
  automation: keep the outcome deterministic — derive the status from the
  automation's response, never from the triage text.
- Need a third attempt? Unroll it: clone `remediate-2` + `verify-2` behind
  the failed leg — the cap stays structural.
- Different second attempt: the retry is its OWN skill dir on purpose —
  give attempt 2 a stronger action (restart → failover) instead of a
  repeat.

## Budget

Per-run LLM spend is bounded: **exactly 2 model calls per run** (triage +
closure — the detect/remediate TOOL nodes, both verification gates, and the
HUMAN routing are deterministic, zero-cost; every path makes the same 2
calls). Cap absolute spend with the agent `budget.max_cost_usd_per_run`
field or a governance COST gate (ADR 093); the eval-gate below is the
quality budget.
