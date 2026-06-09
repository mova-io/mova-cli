# ADR 077 — Agent→workflow dispatch seam + durable-HITL rollout (voice front-door hands off to the durable engine)

**Status:** Accepted — shipped (agent→workflow dispatch + HITL signal endpoint). _(status reconciled to shipped reality 2026-06-08)_
**Date:** 2026-06-05
**Deciders:** Engineering (orchestration/runtime) — **no new shipped dependency;
builds on the already-adopted `temporalio` opt-in (ADR 054/065)**
**Builds on / composes with (changes nothing in any of them):**
ADR 017 (agent orchestration — the native runner, node types incl. HUMAN, ADR 017 D5),
ADR 054 (Temporal as the durable workflow backend — compiler, activities, worker),
ADR 055 (runtime dispatch fork — native / LangGraph / Temporal behind one Protocol),
ADR 062 (durable HITL HUMAN node — `wait_condition` + signal; **HUMAN compile + signal-routed resume landed 2026-06-05; Teams/ServiceNow delivery adapter pending**),
ADR 065 (Temporal as the optional durable-execution seam — native is the floor, adopt per-operation),
ADR 070 (the voice `AgentTurn` seam — the conversational front-door).

**Defining observation (empirical, from the POS-reboot voice demo).** A single
conversational agent asked to *orchestrate* a multi-step, branch-on-result
procedure (check → reboot → re-check → decide → resolve|escalate) is **not
reliable**: in testing, the front-door agent mis-passed a tool argument (sent
the wrong lane to every NCR call) and, separately, reported a still-offline
terminal as "resolved." The same procedure expressed as a **workflow**
(`runtime: temporal`, agent/intent-router/human nodes) was deterministic and
auditable every run. This is exactly the split the reference architecture
already draws — *"Voice AI converses & understands"* then *"MDK agent hands off
to the workflow for reliable execution."* Today, though, there is **no
first-class way for an agent to hand off to a workflow**: an agent can call a
skill, but no skill starts a durable workflow run and reports its outcome back.
This ADR closes that gap and completes the ADR 062 HITL rollout the handoff
depends on.

This is a **strategic / design** ADR (rule 1/2). It introduces a thin seam and
a rollout order; each piece ships in its own PR against this contract.

---

## Decision

### D1 — A `dispatch-workflow` skill: the agent→workflow handoff

Introduce a built-in skill (behind the existing `SkillBackend` Protocol) that
**starts a workflow run** and returns a handle. The conversational agent stays
thin — it extracts entities (store, lane, issue), calls `dispatch-workflow` with
the workflow name + initial state, and narrates the result. The *orchestration*
(retries, branching, HITL) lives in the workflow, where it is deterministic.

```yaml
# In the front-door agent.yaml — one skill, not four:
skills:
  - dispatch-workflow      # starts pos-register-reboot, returns {run_id, status, summary}
```

The skill resolves the durable backend via the **ADR 055 dispatch fork** — it
starts the run on Temporal when configured, native otherwise (same code path,
same `--runtime` override). No new selection logic.

### D2 — Two handoff modes: `await` and `detach`

* **`await`** (default for short flows): the skill blocks until the workflow
  reaches a terminal *or* a HITL pause, then returns the state. The voice agent
  speaks the outcome ("it's back online, ticket INC… closed" / "still down,
  escalating, a technician will call back").
* **`detach`** (long/multi-day flows): the skill returns immediately with a
  `run_id`; the agent tells the caller it's in progress. Resumption + outcome
  delivery ride D3.

The mode is a skill input, not a new seam — the workflow runner already supports
both run-to-completion and pause-at-HUMAN.

### D3 — Complete the ADR 062 durable-HITL rollout (the escalation half)

The HUMAN node core (pause/resume via `wait_condition` + signal) landed; the
**operational rollout did not**. Ship it so an escalation is real:

* **Signal endpoint** — `POST /api/v1/workflows/{run_id}/signal` to resume a
  paused run with the human's resolution (the durable analogue of ADR 017 D5's
  native resume).
* **Pending-approval inventory** — `awaiting_human` run state + a
  `GET …/workflows?status=awaiting_human` list, so operators see every paused
  escalation across tenants.
* **Timeout / escalation policy** — a per-HUMAN-node deadline; on expiry, fire a
  configured action (re-notify, auto-escalate, or fail-closed).
* **Delivery adapter** — a Teams/ServiceNow card (behind a small `Notifier`
  Protocol) carrying the screen-pop context, so the "escalate to human" box is
  an actual hand-off, not just a paused row.

### D4 — Meter + trace the handoff at the activity boundary (unchanged seams)

The dispatched workflow's per-node spans (ADR 024) and per-activity usage
metering (ADR 036) already exist; D1 just threads the parent voice-turn
`trace_id` into the workflow root span so a call and its dispatched workflow
share one trace. Observability is wiring, not new sinks.

---

## Consequences

* The voice/chat front-door becomes a **thin, reliable** entity-extractor +
  narrator; correctness-critical branching moves to the durable engine where it
  belongs. This removes the class of failure the demo surfaced.
* The reference architecture's "hands off to the workflow" arrow becomes a real,
  supported seam instead of a diagram aspiration.
* HITL escalations are operationally complete (inventory + signal + timeout +
  delivery), unblocking any workflow with a HUMAN node — not just this demo.
* Native floor is untouched: with no Temporal configured, `dispatch-workflow`
  runs the workflow in-process (native runner), so the handoff works on a
  zero-infra laptop and upgrades to durable when Temporal is wired.

## Boundaries

* No new shipped dependency; `temporalio` stays the opt-in adopted in ADR 054.
* No change to `agent.yaml` / `workflow.yaml` schema beyond the agent listing the
  `dispatch-workflow` skill and (optional) the mode input.
* Control plane (cli) ⊥ execution plane (runtime) preserved — the skill runs in
  the execution plane; the signal endpoint is a runtime API.

## Alternatives considered

* **Keep orchestrating in the conversational agent** — rejected: empirically
  unreliable for branch-on-result procedures (the defining observation).
* **A bespoke "agent calls workflow" coupling** — rejected: would re-invent the
  ADR 055 selection/override/fallback semantics; D1 reuses the existing fork.
* **Make HITL a native-only feature** — rejected: multi-day escalations need
  durability; the HUMAN node already targets Temporal (ADR 062).

## Scope / rollout

1. **D3 signal endpoint + `awaiting_human` inventory** — smallest, unblocks
   escalations; standalone PR.
2. **D1 `dispatch-workflow` skill (`await` mode)** — the handoff; depends on (1)
   for the pause case.
3. **D2 `detach` mode + D3 timeout/escalation + delivery adapter** — the
   long-running / notification half.
4. **D4 trace threading** — final polish.

Each step ships against this contract; none is a big-bang migration (ADR 065 D4
discipline).
