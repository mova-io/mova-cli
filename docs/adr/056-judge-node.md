# ADR 056 — `JUDGE` as a first-class workflow node (verdict-gated branching + reflection, across all backends)

**Status:** Proposed
**Date:** 2026-05-30
**Deciders:** Engineering (orchestration/runtime)
**Context window:** make eval-gated and reflection workflows first-class —
elevate "judge" from an overloaded `intent-router` (and a Phase-1 state
interpreter) to a real node type that runs a judge and gates on its verdict,
uniformly on the native runner, LangGraph, and Temporal.
**Builds on / composes with (changes nothing in any of them):**
ADR 017 (agent orchestration — the IR, the node types, the native
`WorkflowRunner`),
ADR 030 / ADR 054 / ADR 055 (the three execution backends + the runtime
dispatch fork — a `JUDGE` node must lower onto **all three** behind the one
seam),
ADR 043 (self-improving agent loop — reflection = generate → **judge** →
revise; this node is its workflow primitive),
ADR 008 / the 4-dimension eval reporting (a judge's score is the natural
input to an eval-gate and to a future `reflection_score` dim),
ADR 054 Track C (`temporal_activities.call_judge_activity` — today a
**state interpreter** because the IR has no JUDGE node; this ADR makes that
activity run a real judge).

**Defining gap this ADR closes.** The IR (`core/workflow/ir.py`) models
`AGENT`, `INTENT_ROUTER`, `TOOL`, `HUMAN`, `FUNCTION`, `SUB_WORKFLOW` — **but no
`JUDGE`.** Today a "judge" is expressed as an `intent-router` whose classifier
agent happens to emit a pass/fail-ish `label` (see `pattern_simulation`'s
`turn-judge`). That overload has three costs:

1. **No verdict contract.** An intent-router returns a `label` string; a judge
   needs a structured verdict — *accept/revise*, a numeric **score**, and
   **feedback** the next iteration can consume. `core/reflection.py` already
   defines exactly this (`JudgeVerdict{verdict, score?, feedback}`) but it is
   wired only into the in-Executor reflection loop, **not** into workflow IR.
2. **No eval-gate / reflection primitive.** "Loop until the judge accepts, max
   N times" and "gate the workflow on a quality score" are the two highest-value
   agent patterns (ADR 043), yet a workflow author can only fake them by
   abusing routing labels — with no score to gate on and no feedback to feed
   back.
3. **It already leaked into Temporal.** ADR 054 Track C had to ship
   `call_judge_activity` as a *state interpreter* (it reads a `verdict`/`label`
   already in `state`) precisely because there is no JUDGE node carrying a
   judge-agent ref. That honest-scope caveat (flagged in the Track C docstring
   per CLAUDE.md §11) is a direct symptom of this missing node type.

This ADR adds the node, its verdict contract, and its lowering onto all three
backends. It is **additive and backward-compatible**: existing workflows
(which use no `judge` type) are byte-for-byte unchanged, and the three backends'
existing behavior is untouched for every non-JUDGE node.

---

## Context

Two live, customer-driven patterns converge on the same missing primitive:

- **Eval-gated workflows.** "Run the agent, have a judge score the answer, and
  only continue (publish / hand off / return) if the score clears a threshold —
  otherwise route to a fallback / escalate." Today this is unbuildable without
  abusing `intent-router` and inventing a label convention, and there is no
  score to threshold on.
- **Reflection loops (ADR 043).** Generate → judge → (revise with the judge's
  feedback) → judge again, bounded by a max-iteration cap. `core/reflection.py`
  implements this *inside* the Executor for a single agent; there is no way to
  express it as a **workflow** spanning multiple agents, and therefore no way to
  run it on LangGraph/Temporal or to score it in eval.

Both want the same thing: a node that runs a judge, produces a *structured
verdict with a score and feedback*, and lets the workflow **branch or loop** on
it. `intent-router` is the wrong tool (label-only, no score, no feedback);
`HUMAN` is the wrong tool (a human, not a model). The right tool is a dedicated
`JUDGE` node — and because mdk now has three execution backends behind one seam
(ADR 055), it must lower onto all three identically.

## Decision

Add `JUDGE` as a first-class node type with a structured verdict contract, a
native execution path, and a lowering for each backend. Reuse
`core/reflection.py`'s judge semantics — no second judge implementation.

### D1 — `JUDGE` in the IR + a `JudgeNodeSpec` in the schema

- `core/workflow/ir.py`: add `JUDGE = "judge"` to `NodeType` (additive enum
  member; promotes the judge concept to v1.x alongside `AGENT`/`INTENT_ROUTER`).
- `core/workflow/spec.py`: add `JudgeNodeSpec` to the `NodeSpec` discriminated
  union (`Field(discriminator="type")`, `type: Literal["judge"]`), peer to
  `AgentNodeSpec` / `IntentRouterNodeSpec` / `HumanNodeSpec`. Fields:
  - `judge_agent: str` — ref to the judge agent (path, resolved like every other
    node ref), OR an inline `criteria` that reuses `reflection.py`'s default
    judge prompt when no custom judge agent is supplied.
  - `input_field: str` — the `state` key holding the artifact to judge
    (mirrors `IntentRouterNodeSpec.input_field`).
  - `pass_threshold: float | None` — when set, `score >= threshold` ⇒ *accept*
    (the eval-gate form). When unset, the judge's categorical `verdict`
    (*accept*/*revise*) drives the gate (the reflection form).
  - routing: `on_accept` / `on_revise` next-node ids (the eval-gate/branch
    form), and/or participation in a bounded back-edge loop (the reflection
    form — D4).

> Compat note (flagged per CLAUDE.md rule 5): additive `workflow.yaml`/spec
> schema change. `extra="forbid"` means no existing workflow declares `judge`
> today; the new node type and its spec are purely additive, default behavior
> for every existing workflow is unchanged.

### D2 — The verdict contract (reuse `reflection.JudgeVerdict`)

A JUDGE node produces a single canonical verdict object — **the same one
`core/reflection.py` already defines**, surfaced into workflow `state`:

```
{ "verdict": "accept" | "revise" | "parse_error",
  "score": float | null,        # 0..1 when the judge emits one
  "feedback": str,              # what to fix — consumed by the revise step
  "terminate": bool }           # derived: accept (or score>=threshold) ⇒ true
```

`terminate` is the derived field the backends gate on (it is exactly what the
Temporal compiler's `_emit_judge_node` already expects:
`if verdict.get('terminate'): ...`). There is **one** verdict shape; no backend
invents its own.

### D3 — Native execution: run the judge through the Executor

The native `WorkflowRunner` gains a JUDGE branch in `_walk`: load the
`judge_agent` bundle, project the `input_field` from `state`, run it through the
**same `Executor.execute(...)`** every other node uses (so tracing, metering,
session, BYOK all flow through the one place — the boundary rule), parse the
result into the D2 verdict via `reflection.py`'s existing parser, stamp it into
`state`, and branch on `terminate` (`on_accept`/`on_revise`) — or, in a loop,
fall through to the bounded back-edge (D4). No new judge engine: this is the
`reflection.call_judge` semantics, lifted to the workflow layer.

### D4 — Reflection = JUDGE + a bounded loop

The reflection pattern is *not* new machinery — it is a JUDGE node on a
back-edge with a `max_iterations` bound (the same bound the compilers already
enforce for cycles, ADR 030 D2 / ADR 054 D4): `produce → judge → (revise →
produce)* → accept|cap`. The cap is mandatory (failure-mode rule: a judge that
never accepts must not loop forever). The judge's `feedback` is threaded into
the revise agent's input. This makes ADR 043's self-improving loop a declarable,
testable, backend-portable workflow shape, and ships as a `reflective-agent`
template (closing the half-done backlog item: reflection engine exists, the
template + `reflection_score` dim did not).

### D5 — Backend lowering (all three, behind ADR 055's seam)

- **Native** — D3.
- **Temporal** — the compiler's `_emit_judge_node` (already the canonical
  shape, currently unused) goes live, and **Track C's `call_judge_activity`
  gains the `judge_agent` ref** so it runs the judge through the Executor
  instead of interpreting a pre-existing state value. This *resolves* the Track
  C §11 caveat. The activity returns the D2 verdict; the workflow gates on
  `terminate` (recorded in history ⇒ deterministic replay, ADR 054 D4 row 4).
- **LangGraph** — a conditional edge driven by the verdict (`add_conditional_
  edges` on `terminate`/`verdict`), with the bounded loop as a recursion-limited
  cycle (ADR 030 D2).

All three pass the **conformance suite** (ADR 055 D7): the same JUDGE workflow
on the same fixture yields the same verdict-driven trajectory.

### D6 — Eval integration: the score is the gate input

A JUDGE node's `score` is the natural feed for (a) a workflow eval-gate and
(b) a future `reflection_score` eval dim (did the critique actually improve the
output across iterations?). This ADR *reserves* that integration; the dim itself
is a follow-up that plugs the JUDGE verdict series into the existing 4-dim eval
machinery. No eval-schema change is forced here.

## Consequences

**Positive**
- Eval-gated and reflection workflows become **declarable, portable, and
  testable** — the two highest-value agent patterns stop being label hacks.
- One verdict contract + one judge implementation (`reflection.py`) reused
  everywhere; Track C's state-interpreter caveat is resolved, not papered over.
- `intent-router` goes back to meaning *only* intent routing — no overload.

**Negative / risks**
- **A new node type is new surface on three backends** — mitigated by reusing
  `reflection.py` (no new judge logic) and by the ADR 055 conformance suite as
  the equivalence gate.
- **Judge cost / latency** — every JUDGE node is an extra model call; it is
  metered like any other (ADR 036), and the bounded loop (D4) caps the blast
  radius.
- **Judge reliability** — a flaky judge mis-gates; `parse_error` is a defined
  verdict (soft-accept, never crash), mirroring `reflection.py`'s existing
  fail-open posture.

## Boundaries

`JUDGE` is a node behind the IR/runner seam; `core` depends on the seam, not on
any backend; the judge runs through the existing `Executor` (tracing/metering at
the edges, never imported into execution logic). Additive, behavior-preserving
default; no storage-schema change. Reuses `reflection.py` — adapt, don't
reinvent (ADR 017).

## Alternatives considered

- **Keep overloading `intent-router`.** Rejected — no score, no feedback, and it
  conflates "which intent" with "is this good enough." The Track C state-
  interpreter caveat is the concrete tax of this status quo.
- **Keep judging Executor-internal only (`reflection.py`).** Rejected — that
  cannot span multiple agents, cannot run on LangGraph/Temporal, and cannot be
  scored at the workflow level. The pattern needs to be a *workflow* primitive.
- **A generic `FUNCTION`/predicate node instead of a JUDGE.** Rejected — a judge
  is specifically an LLM-graded verdict with a score + feedback contract; a bare
  predicate loses the score/feedback that eval-gating and reflection require.

## Scope / rollout

Multi-PR; this ADR is doc-only.

1. **IR + spec** — `JUDGE` enum + `JudgeNodeSpec` + validation (additive).
2. **Native execution** (D3) + the `reflective-agent` template + the reflection
   loop (D4) on the native runner — ships value with zero backend dependency.
3. **Backend lowerings** (D5) — Temporal (`_emit_judge_node` live +
   `call_judge_activity` gains the ref) and LangGraph conditional/cycle — each
   gated by the ADR 055 conformance suite.
4. **Eval** (D6) — the `reflection_score` dim + workflow eval-gate on the JUDGE
   score (with ADR 008).
