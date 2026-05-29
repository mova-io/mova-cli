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

---

## Functional Patterns — the concrete first slice

The taxonomy + governance contract above are the *primitives*. This section maps
the five **Functional Patterns** customers actually ask for — **Chatbot · Task
Oriented · Goal Oriented · Monitor · Simulation** — onto those primitives as
*governed, bounded topologies*. Each is expressed in D1's typed nodes, carries
the D2 governance contract (budgets · bounds · gates · full trace), and ships as
a scaffoldable, catalog-discoverable template. They are the **concrete first
slice** being implemented against this otherwise north-star ADR — the companion
implementation PR ships the five pattern templates that realize this section.
The ADR stays **Proposed** (the library as a whole is still a direction, not a
committed milestone); these five are the part of it now being built.

Crucially, none of the five is a new engine or a raw agent-spawning loop. Each
is a `WorkflowSpec` over the existing `WorkflowRunner`, so D5's governable filter
holds by construction: every pattern is observable (per-node spans, ADR 024),
evaluable (workflow evals, ADR 008; inline JUDGE, D4), bounded (budget + depth +
iteration + turn caps, D2), and deployable (registry + durable execution, ADR
017).

### F1 — Chatbot
- **Topology:** `INPUT → AGENT → OUTPUT` — optionally multi-turn via the
  playground / sessions (each turn is one pass through the same DAG, with session
  state threaded as typed state).
- **Governance contract:** per-run **budget cap** (cost/token) + **output-schema
  VALIDATE** on the AGENT result; per-turn trace span. No delegation, no looping.
- **MDK capability today:** **shipped** — this is the default single-agent shape
  (`chatbot`/`faq` templates) plus the playground/sessions loop. Nothing new is
  required; it is included to anchor the spectrum at its simplest governed form.
- **Scaffold path:** `mdk init --pattern chatbot` (today: the default agent
  templates, ADR 028).

### F2 — Task Oriented
- **Topology:**
  `INPUT → SUPERVISOR(plan/decompose) → [AGENT … parallel branches] → (collect) → OUTPUT`.
  A bounded SUPERVISOR (D4) decomposes the task into a **fixed** set of typed
  branches, fans them out in parallel, and a collect/reduce step joins results.
- **Governance contract:** **max fan-out width** (delegate allowlist + branch
  cap), **per-branch budget + total budget cap**, each branch independently typed
  and traced, deterministic collect. No recursive re-decomposition beyond
  max-depth.
- **MDK capability today:** a `WorkflowSpec` (ADR 017) with parallel branches +
  the **bounded SUPERVISOR** (ADR 038 D4); workflow authoring (ADR 029) wires the
  fan-out/collect edges.
- **Scaffold path:** `mdk init --pattern task` → a `workflow.yaml` with a
  SUPERVISOR node, N typed AGENT branches, and a collect node.

### F3 — Goal Oriented
- **Topology:**
  `INPUT → SUPERVISOR ⇄ JUDGE/GATE (goal-satisfaction loop) → OUTPUT`.
  The SUPERVISOR drives sub-agents toward a goal; after each iteration an inline
  **JUDGE** scores goal-satisfaction and a **GATE** is the loop-exit — pass →
  OUTPUT, fail → another bounded iteration.
- **Governance contract:** **max-iterations cap** and **budget cap** as the hard
  stops; the **GATE is the only loop-exit** (no unbounded looping — a tripped
  bound exits to OUTPUT or HUMAN, never silently spins); **full per-iteration
  trace** with the JUDGE verdict on each pass.
- **MDK capability today:** the inline **JUDGE/GATE** flagship (D4) + the bounded
  SUPERVISOR + D2's max-iteration guard; this is "bounded reflection" (Tier-2)
  expressed as a goal loop.
- **Scaffold path:** `mdk init --pattern goal` → a `workflow.yaml` with the
  SUPERVISOR↔JUDGE/GATE loop and an explicit `max_iterations` + budget.

### F4 — Monitor
- **Topology:**
  `(schedule / trigger) → INPUT → AGENT(observe) → VALIDATE/GATE(threshold) → TOOL(react/alert)`.
  A scheduled or event-driven entrypoint feeds an observe-AGENT; a VALIDATE/GATE
  applies a threshold; on breach a TOOL step reacts (alert / file / remediate).
- **Governance contract:** a fixed **scheduled cadence** (or typed trigger), an
  **action-allowlist on the TOOL step** (it can only invoke pre-approved
  reactions), and an **audit record for every triggered action**. The GATE means
  no action fires below threshold.
- **MDK capability today:** the native **scheduler + event/webhook triggers**
  (ADR 017 D2) + **continuous-eval / drift** (ADR 016 D2) as the threshold
  source. The **observability analyst (ADR 047)** is itself a Monitor-pattern
  agent — a scheduled observe-AGENT over telemetry whose anomalies gate typed
  actions — i.e. the pattern already exists in production form.
- **Scaffold path:** `mdk init --pattern monitor` → a scheduled `workflow.yaml`
  with the observe → threshold → allowlisted-action chain.

### F5 — Simulation (bounded multi-agent — NOT a swarm)
- **Topology:**
  `INPUT → SUPERVISOR(orchestrates a FIXED participant set) → [AGENT ↔ AGENT bounded turns] → JUDGE → OUTPUT`.
  A SUPERVISOR runs a **fixed roster** of participant AGENTs through bounded
  back-and-forth turns; a terminating JUDGE evaluates the interaction and a GATE
  ends it.
- **Governance contract — the whole point:** **fixed participant roster** (no
  dynamic agent-spawning, ever), a **hard turn cap**, a **hard budget cap**,
  **full interaction trace** (every turn is a span), and a **terminating
  JUDGE/GATE** as the only exit. This is the *governable* answer to multi-agent
  interaction — see "Why bounded" below.
- **MDK capability today:** the bounded SUPERVISOR (D4) + inline JUDGE/GATE (D4)
  + D2's turn/budget caps over a `WorkflowSpec`. No new engine; the roster is
  declared statically in `workflow.yaml`, so there is no spawning surface.
- **Scaffold path:** `mdk init --pattern simulation` → a `workflow.yaml` with a
  declared participant roster, `max_turns`, a budget cap, and the terminating
  JUDGE.

### Mapping table

| Pattern | Topology (D1 nodes) | Governance contract (D2/D4) | MDK capability today | Scaffold path |
| --- | --- | --- | --- | --- |
| **Chatbot** | `INPUT → AGENT → OUTPUT` (multi-turn via sessions) | per-run budget + output-schema VALIDATE | **shipped** — default agent + playground/sessions | `mdk init --pattern chatbot` |
| **Task Oriented** | `INPUT → SUPERVISOR(plan) → [AGENT… ∥] → collect → OUTPUT` | max fan-out width · per-branch + total budget cap · each branch typed + traced | workflow (ADR 017) + bounded SUPERVISOR (D4) + authoring (ADR 029) | `mdk init --pattern task` |
| **Goal Oriented** | `INPUT → SUPERVISOR ⇄ JUDGE/GATE → OUTPUT` | max-iterations cap · budget cap · GATE = sole loop-exit · per-iteration trace | inline JUDGE/GATE (D4) + bounded SUPERVISOR + max-iteration guard (D2) | `mdk init --pattern goal` |
| **Monitor** | `(schedule/trigger) → INPUT → AGENT(observe) → VALIDATE/GATE → TOOL` | scheduled cadence · action-allowlist on TOOL · audit every triggered action | scheduler + triggers (ADR 017 D2) + drift (ADR 016 D2); analyst (ADR 047) is one | `mdk init --pattern monitor` |
| **Simulation** | `INPUT → SUPERVISOR(fixed roster) → [AGENT ↔ AGENT bounded turns] → JUDGE → OUTPUT` | fixed roster (no spawning) · hard turn cap · hard budget cap · full trace · terminating JUDGE/GATE | bounded SUPERVISOR (D4) + inline JUDGE/GATE (D4) + turn/budget caps (D2) | `mdk init --pattern simulation` |

### Why bounded — these are governed topologies, not raw agent-spawning

Every Functional Pattern is shipped as a **governed, bounded topology** —
declared typed nodes + budgets + eval-gates + full trace — and **never** as raw
agent-spawning or an open collaboration loop. This is D5's governable filter
applied to the pattern catalog: a pattern earns a template only if it is
observable, evaluable, bounded, and deployable. The bounds are not a limitation
bolted on after the fact; they are *what makes the pattern a product*.

**Simulation is the sharpest example.** ADR 038 already **declines the open
organizational-mesh / swarm / recursive-spawning quadrant** (Tier-3 / Scope-out
above): uncontrolled multi-agent collaboration can't be cleanly governed,
evaluated, or cost-bounded. The Functional-Pattern answer is *not* to relax that
stance — it is to ship the **bounded** form that delivers the multi-agent value
*with* governance: a **fixed participant roster** (so there is no spawning
surface at all), a **hard turn cap** and **budget cap** (so cost and runtime are
provably bounded), a **terminating JUDGE/GATE** (so it always ends on a verdict,
never spins), and a **full interaction trace** (so every turn is auditable).
Bounded-Simulation is therefore the *governable* counter-proposal to the swarm,
not a partial adoption of it. The same logic governs Task (fixed fan-out, no
recursive re-decomposition) and Goal (the GATE is the only loop-exit).

### Scaffold + catalog story

These five ship as **governed pattern templates**, not as a new framework:

- **Scaffoldable** via `mdk init --pattern <chatbot|task|goal|monitor|simulation>`,
  which extends the ADR 028 template gallery (use-case metadata + the `workflow`
  starter) and the ADR 029 workflow-authoring spine (the same catalog → planner →
  driver path builds and verifies each topology). Each `--pattern` resolves to a
  bundled `WorkflowSpec` template carrying its budgets, caps, and gate nodes
  pre-wired, so the scaffold is governed on day one.
- **Discoverable / cloneable** via the **catalog (ADR 041)** — the five patterns
  are first-class catalog entries (Movate-curated namespace), browseable and
  pullable into a project exactly like any agent template, inheriting the ADR 028
  use-case taxonomy.
- **Realized by the companion implementation PR** — the build that ships the five
  pattern templates (the `WorkflowSpec` scaffolds + their pre-wired governance
  contracts + catalog metadata) is the concrete first slice of this otherwise
  north-star ADR. This ADR is the design; that PR is the implementation.

### Open questions / boundaries (Functional Patterns)

- **Still declining open swarms.** The Scope-out + Tier-3 stance is unchanged —
  uncontrolled swarms, autonomous collaboration, debate, and recursive spawning
  remain declined / sandbox-only. The Functional Patterns do **not** reopen them.
- **Simulation v1 is bounded-only.** A fixed roster, hard turn/budget caps, and a
  terminating JUDGE/GATE are mandatory; there is no "unbounded" mode.
- **Dynamic agent-spawning is out.** No pattern may spawn agents at runtime; the
  participant set (Simulation) and the branch/fan-out set (Task) are declared
  statically in `workflow.yaml`. A future federation/multi-tenant story (already
  flagged in Scope-out) may revisit a subset much later — out of scope here.
- **Open question — per-pattern default caps.** The *shipped* default budgets,
  turn/iteration caps, and fan-out widths per template are an implementation
  detail for the companion PR to set (and for customers to override), not fixed
  by this ADR.
