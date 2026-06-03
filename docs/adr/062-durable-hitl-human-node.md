# ADR 062 — Durable human-in-the-loop (HUMAN node) on Temporal

**Status:** Accepted
**Date:** 2026-05-31
**Deciders:** Engineering (workflow runtime)
**Context window:** promote the HUMAN workflow node from a Phase-1 stub to a
**durable pause/resume** on the Temporal backend — a workflow can wait *days or
weeks* for a human decision (approve / reject / edit), survive worker + runtime
restarts in between, and resume exactly where it paused. This is the headline
enterprise-workflow capability (refund approval, content sign-off, compliance
gate) and ADR 054 Phase 2.
**Builds on / composes with (changes nothing in any of them):**
ADR 054 (Temporal as a durable backend — names `workflow.wait_condition` +
`signal` as the durable HITL primitive; the HUMAN node was explicitly deferred
to Phase 2), ADR 017 D5 (the **native** runner's HUMAN pause/resume — paused
state persisted via `StorageProvider.save_workflow_run`, resumed by re-walking
from the gate's successor; this ADR mirrors its *semantics* on a durable
substrate), the Tier-0 branching fix (#653 — which stubbed the HUMAN node with
`NotImplementedError` + the `TEMPORAL_HUMAN_NODE_PHASE2` lint warning and built
the real dispatch loop this node plugs into), the existing
`POST /api/v1/workflow-runs/{id}/signal` resume transport, and ADR 003 (Teams
Adaptive Cards — the natural first human-facing transport).

**Defining gap.** On Temporal, a HUMAN node currently raises
`NotImplementedError` at compile time (Phase 1). The native runner *can* pause
for a human (ADR 017 D5) but persists a re-walkable snapshot — fine for
hours-to-a-day, strained by multi-week pauses with runtime restarts in between.
Temporal's `wait_condition` + `signal` are *natively* durable across restarts
for arbitrary spans, which is exactly the enterprise-approval shape. This ADR
makes the Temporal HUMAN node real.

This is a **design** ADR for a runtime execution primitive (rule 2). The node
semantics are **additive** — workflows without a HUMAN node are unaffected; the
native runner is unchanged; the wire/API surface reuses the existing signal
endpoint.

---

## Decision

### D1 — HUMAN compiles to `wait_condition` + a typed signal

The Temporal compiler emits, for a HUMAN node:

```python
@workflow.signal
def human_response(self, payload: dict) -> None:
    self._human[node_id] = payload          # idempotent: last write wins

# at the node:
emit_pause_record(node_id, prompt, approvers, deadline)   # via an activity
await workflow.wait_condition(
    lambda: node_id in self._human,
    timeout=deadline_timedelta,             # None = wait forever
)
response = self._human.pop(node_id)
# response routes the gate-style branch (approve→routes['approve'], …) and
# merges declared fields into state — same dispatch loop the Tier-0 fix built.
```

The pause is **durable**: `wait_condition` parks the workflow in Temporal's
event history; a worker/runtime restart re-hydrates it and keeps waiting. No
poller, no re-walk, no busy state.

### D2 — Resume reuses the existing signal transport

`POST /api/v1/workflow-runs/{id}/signal` (already shipped) is the resume door.
For a Temporal run it calls the Temporal client's `signal` to deliver
`human_response`; for a native run it drives the ADR 017 D5 resume path. One API,
both backends — the caller (Teams card handler, email-reply webhook, a UI button,
or curl) doesn't know or care which backend is underneath. Body:
`{node, decision, fields?}` (`decision` ∈ the node's declared routes;
`fields` merge into state). Scope: `run` (the same scope that submits runs);
the signal is tenant-scoped at the run lookup (a cross-tenant run id 404s).

### D3 — The HUMAN node spec (what the human sees + sends back)

The `workflow.yaml` HUMAN node declares: `prompt` (the question rendered to the
approver), `routes` (`approve` / `reject` / … → next node, reusing the gate
routing the Tier-0 fix added), optional `approvers` (principals/role allowed to
respond — enforced at the signal endpoint), optional `timeout` + `on_timeout`
(a route taken if no one responds in time, D4). The pause record (D1's
`emit_pause_record` activity) persists this to `StorageProvider` so an operator
can **list pending approvals** (`GET …/workflow-runs?status=awaiting_human`) and
a transport can render the card.

### D4 — Timeout + escalation

`wait_condition(timeout=…)` gives a durable deadline. On expiry the node takes
`on_timeout` (a declared route — e.g. `escalate` or `auto-reject`) instead of
hanging forever. `timeout: null` = wait indefinitely (explicit opt-in). This is
the durable analogue of a human SLA.

### D5 — Native parity + conformance

The conformance suite (the one that caught Tier-0) gains a HUMAN fixture:
drive the same `{decision}` into native and Temporal and assert identical final
state + branch. The native runner already implements the semantics (ADR 017 D5);
this ADR makes Temporal match it node-for-node, just *durably*. The Phase-1
`TEMPORAL_HUMAN_NODE_PHASE2` lint warning is removed.

### D6 — Transports (compose, don't hardcode)

The resume endpoint is transport-agnostic. First-class: **Teams Adaptive Card**
with Approve/Reject buttons (ADR 003 — the card's `Action.Submit` POSTs the
signal), **email reply** (an inbound webhook maps a reply to a decision), and a
**plain API/UI button**. New transports are new callers of the same endpoint —
no runtime change.

### D7 — Backward compatibility (additive)

Workflows with no HUMAN node compile + run exactly as today. The native runner
is untouched. The signal endpoint already exists (its shape is extended
additively with the optional `node`/`fields`). New `workflow.yaml` HUMAN fields
are optional with safe defaults. No storage-schema change beyond the pause
record, which reuses the existing `workflow_run` row's state/status (a new
`awaiting_human` status value — additive enum). No version bump in a PR
(ADR 059).

## Consequences

**Positive**
- Real **durable HITL** — approvals that survive weeks + restarts, the
  enterprise-credibility feature Temporal was adopted for (ADR 054).
- **One resume API** across native + Temporal; **many transports** (Teams,
  email, UI) for free.
- Removes the last Phase-1 stub; the Temporal backend now covers the full node
  taxonomy (linear, gate, judge, supervisor, bounded loop/fan-out, **human**).

**Negative / risks**
- A workflow can park indefinitely — bounded by D4 timeouts + the pending-approval
  list so a stuck pause is *visible*, not silent (rule 10).
- Signal idempotency: a double-click / retried card POST must not double-resume —
  `wait_condition` fires once on first matching signal; later signals for a
  resumed node are dropped (last-write-wins before resume, no-op after).
- Approver authz lives at the endpoint (D3) — a misconfigured `approvers` list
  could over- or under-restrict; defaults to "any principal with `run` on the
  tenant" when unset.

## Boundaries

Execution primitive on the Temporal backend (rule 6) — the resume *transport*
is at the API edge, the *durability* is Temporal's, the *semantics* mirror the
native runner. No new dependency. Additive node + additive signal fields +
additive status value. Mirrors ADR 017 D5 rather than inventing a new pause model.

## Alternatives considered

- **Poll a "paused" row from an external scheduler.** Rejected — that's the
  native re-walk model, which strains at multi-week pauses with restarts; the
  whole point of the Temporal backend is native durability (ADR 054).
- **A bespoke pause/resume store + bespoke resume endpoint.** Rejected —
  duplicates the existing `workflow-runs/{id}/signal` transport and the
  `workflow_run` row; one door, two backends is the parity contract.
- **Make HUMAN a SUPERVISOR-orchestrated callout.** Rejected — loses the
  declarative `workflow.yaml` node + the pending-approval inventory + the
  native parity; HUMAN is a first-class node (ADR 017 D5), not orchestration glue.

## Scope / rollout

1. Compiler: HUMAN node → `wait_condition` + `@workflow.signal` (D1); remove the
   Phase-1 stub + lint warning.
2. Resume: wire `POST /workflow-runs/{id}/signal` to deliver the Temporal signal;
   `awaiting_human` status + pause record + pending-approval listing (D2/D3).
3. Timeout/escalation route (D4) + the conformance HUMAN fixture (D5).
4. Teams Adaptive Card transport (D6) — composes with ADR 003.
