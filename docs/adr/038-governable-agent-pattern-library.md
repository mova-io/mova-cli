# ADR 038 — Governable agent-pattern library + orchestration primitives (north-star)

**Status:** Proposed — **backlog / north-star, NOT scheduled.** Built
*incrementally* by ADR 028 (templates), 029 (workflow authoring), 030 (compiler),
and the eval/judge layer — this ADR records the *direction* those build toward,
not a committed milestone.
**Date:** 2026-05-27
**Deciders:** Engineering + Deva (Movate)
**Builds on / related:** `core/workflow/` (`WorkflowSpec`/`WorkflowRunner`/node
types), ADR 008 (workflow evals), ADR 016 (judge/drift), ADR 017 (durable +
HITL + queue + triggers), ADR 023 (auto-RAG), ADR 024 (per-step spans), ADR
028/029/030 (templates / authoring / compiler), the skills layer
(python/http/mcp + SkillPolicy).

## Context
As MDK matures into an enterprise-grade *orchestration + governance + evaluation
+ deployment* framework (not an "autonomous agents" framework), the set of
**agent patterns** it supports is the platform differentiation. The trap is
building N bespoke pattern-engines (swarms, debate, recursive spawning) that demo
well and productionize poorly. The opportunity is a **small set of *governable*
primitives** that compose into the patterns enterprises actually run — each
observable, evaluable, bounded, and deployable.

## Decision
Support agent patterns as **compositions of a governed primitive set over the
existing `WorkflowSpec`/`WorkflowRunner`**, plus a shipped template library — NOT
as separate engines.

- **D1 — Canonical node taxonomy.** Extend `WorkflowSpec` nodes toward:
  `INPUT · RETRIEVE · AGENT · TOOL · VALIDATE · JUDGE · GATE · HUMAN · OUTPUT ·
  SUPERVISOR(delegation)`. (Today: agent / intent-router / human + edges.)
- **D2 — Cross-cutting governance contract on every node/edge** (the real moat):
  typed state, retry policy, **budget (cost/token) caps**, **max-depth +
  max-iteration guards**, checkpoint / durable-resume, deterministic replay,
  policy/scope, trace span. Most exist scattered (tool-loop cap, HITL, cost
  records, idempotency) — unify them as *declarative + uniform*.
- **D3 — Pattern/template library.** Tier-1 patterns shipped as ready
  `WorkflowSpec` templates (ADR 028) + buildable via the copilot (ADR 029):
  sequential pipeline, tool-calling, RAG, HITL, judge-gated, supervisor,
  bounded-reflection.
- **D4 — Two flagship differentiators:**
  - **Inline `JUDGE`/`GATE` nodes** — judges/policy gates *inside the live
    pipeline* (block / escalate-to-human / route on failed
    factuality/groundedness/policy), not only CI-time eval. Promotes "eval" to
    "enforced runtime governance."
  - **Governed (bounded) `SUPERVISOR` delegation** — a manager delegating to
    specialists *with* a delegate allowlist + max-depth + cost budget + canonical
    state. The bounds are the point (vs AutoGen-style entropy).
- **D5 — The governable filter.** A pattern earns inclusion ONLY if it is
  **observable + evaluable + bounded/governable + deployable**. Anything that
  fails (uncontrolled swarms, recursive spawning, debate) is Tier-3 sandbox or
  declined.

## Prioritization (record)
- **Tier 1 (core, production-grade):** sequential · tool-calling · retrieval ·
  HITL · LLM-as-judge (incl. inline gate) · durable execution · event-driven.
  *(MDK is ~80% here today.)*
- **Tier 2 (differentiators):** bounded supervisor · bounded reflection ·
  multi-model routing · policy gates · parallel branches.
- **Tier 3 (experimental, sandbox/decline):** swarms · autonomous collaboration ·
  debate · recursive spawning.

## Scope-out (explicit)
The "Agentic Mesh" **Organizational** quadrant — Ecosystem / Federation / Legal
Entity / Supply Chain — is multi-org/marketplace topology that doesn't fit a
single-enterprise workflow framework and can't be cleanly governed/evaluated.
**Declined** for the foreseeable roadmap (a federation/multi-tenant story may
revisit a subset much later). The Agentic-Mesh **Role** quadrant
(Planner/Orchestrator/Executor/Observer/Judge/Enforcer) is adopted only as
*naming* that maps onto D1's node taxonomy + the judge/policy layer.

## Consequences
**Positive:** patterns ship as governed recipes, not engines; reuses
`WorkflowSpec`+runner+eval+HITL+observability; every pattern is auditable,
boundable, and deployable — the enterprise wedge. **Negative/risk:** scope creep
into a general workflow/ETL platform — mitigated by D5 (the governable filter)
and "primitives + templates, never bespoke engines."

## Boundaries
No new execution engine; `cli ⊥ runtime`; reuse the existing runner, eval/judge,
HITL, registry, and observability. Positioning: **enterprise orchestration +
governance + evaluation + deployment**, not the autonomous-agent race.
