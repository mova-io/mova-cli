# ADR 024 — Per-step execution observability: nested spans, retained per-step cost/latency, and a CLI tree

**Status:** Proposed
**Date:** 2026-05-26
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x observability inner loop — make a run's *internal* shape
(every LLM turn, every skill/tool call, every KB retrieval) first-class and legible
both in the trace backend and offline in `mdk explain`, so "where did the time/cost
go in this run?" is answerable at a glance.
**Related / constrained by:** ADR 002 (skills are LLM-invoked tools dispatched in the
Executor loop; skill `cost.per_call_usd` participates in budget), ADR 015 (Tracer
seam; Langfuse/OTLP are deployment choices, tracing is optional), ADR 019 (W3C trace
context propagated across the queue → one distributed trace per async run), ADR 020
(OTLP → collector → App Insights), ADR 023 (the auto-RAG pre-retrieval phase — a new
retrieval step this model must also span/retain). Touches `core/executor.py`,
`core/models.py` (`RunRecord`/`SkillCallRecord`/`Metrics`), `tracing/*` (no Protocol
change), `cli/explain.py`, `cli/trace.py`, and the workflow orchestrator.

## Decision

Adopt a **per-step execution model** — *turns* (one LLM round-trip) containing *skill
calls* and *retrievals* — and surface it three ways, all reading the same model:

1. **(D1) Nested spans in the Executor.** Replace the single flat `agent.execute`
   span + flat events with a span **tree**: `agent.execute` → `agent.turn[i]` →
   `skill.<name>` / `retrieval.<skill>` children, each carrying cost / latency /
   token attributes. Uses the **existing** `SpanCtx.parent` seam — **no Tracer
   Protocol change**.
2. **(D2) Retain the same hierarchy in the persisted `RunRecord`, offline-first.**
   Add `cost_usd` to `SkillCallRecord` and a new `RunRecord.turns: list[TurnRecord]`
   (model, tokens, cost, latency per LLM round-trip). `mdk explain` reconstructs the
   per-step breakdown **without** a Langfuse/OTel backend (the same rationale that
   already justifies persisting `skill_calls`).
3. **(D3) Render it as a tree in the CLI.** `mdk explain` and `mdk trace replay`
   render turns + tool calls + retrievals as a Rich `Tree` with per-node
   cost/latency/tokens; the existing flat table + `--json` stay as fallbacks.
4. **(D4) Workflow trace correlation.** Nest each node's `agent.execute` span under a
   workflow-root span so a workflow is **one** trace tree; `workflow_run_id`/`node_id`
   (already on `RunRecord`) reconstruct the same tree offline.

The aggregate `Metrics` shape is unchanged (it becomes the honest **sum** of per-step
costs). Everything is additive and backward-compatible: a run with no turns/skills
renders exactly as today.

## Context

`mdk explain` answers "what did this run do?" but only coarsely. Today:

- The Executor opens **one** span, `agent.execute` (`core/executor.py:242`), and emits
  flat `log_event(...)` calls and a single `log_generation(...)` for the completion
  (`executor.py:685`). `log_generation` is a **no-op on non-Langfuse tracers**
  (`tracing/base.py:46-57`), so per-turn detail only exists in Langfuse, and even
  there the turns/skills/retrievals are not a nested span tree — they're siblings or
  events under one root.
- The persisted `Metrics` (`core/models.py:1320-1331`) is a **run-level aggregate**:
  one `latency_ms`, one `cost_usd`, one `TokenUsage`. There is **no per-turn record**.
- `SkillCallRecord` (`models.py:1485-1506`) captures `step`, `skill`, `input`,
  `output`, `error`, `latency_ms` — but **no `cost_usd`**. So a multi-skill run can't
  show which tool call cost what, even though skill cost already feeds the budget
  (ADR 002).
- `mdk explain --steps` (`cli/explain.py`) renders `skill_calls` as a **flat table**
  (`_render_skill_calls`); there is no turn/tool/retrieval **tree**, and nothing ties
  a skill call to the LLM turn that requested it.
- Workflows: ADR 019 already makes an async run one distributed trace across the
  queue, and `RunRecord.workflow_run_id`/`node_id` link records — but node-run spans
  are **disconnected roots**, not children of a workflow-root span, so a multi-node
  workflow isn't one legible tree.

ADR 002 anticipated richer per-step accounting ("skill `per_call_usd` is added to
`RunRecord.metrics.cost_usd`") but stopped at the aggregate. ADR 023 adds a
*pre-retrieval* step that also deserves a span + a retained line. The three open
tickets (#99 nested spans + workflow correlation, #100 per-step cost/latency
retention, #101 CLI tree) are **three faces of one model**; deciding them together
avoids three incompatible shapes for "a step."

## Decisions in detail

### D1 — Nested spans, on the existing seam
The Executor creates child spans using the `parent` argument the `Tracer` Protocol
**already** exposes (`start_span(name, attrs, parent=...)`, `SpanCtx.parent_id`):

```
agent.execute (root)
├── agent.turn[1]                      # one LLM round-trip
│   ├── retrieval.kb-vector-lookup     # ADR 023 pre-retrieval (turn 0) or model-driven
│   └── skill.<name>                   # each tool call the model made this turn
├── agent.turn[2]
│   └── skill.<name>
└── …
```

Each span carries `cost_usd`, `latency_ms`, and token attributes. The instrumentation
sits in the Executor's existing loop (around the per-turn `provider.complete` and the
`dispatch_skill`/retrieval call sites) — **tracing stays wired at the edges**
(CLAUDE.md §6); nothing new is imported into deeper logic, and **no new Protocol
methods** are added (the seam already supports nesting). Non-Langfuse/Null tracers
keep no-op'ing — child spans cost nothing when tracing is off.

### D2 — Retain the hierarchy in the record (offline-first)
The trace backend is optional (ADR 015), but `mdk explain` must always work. So the
same per-step data is persisted on `RunRecord`:

- `SkillCallRecord` gains `cost_usd: float = 0.0` (additive; matches the existing
  `latency_ms`).
- New `TurnRecord` (`model`, `input_tokens`, `output_tokens`, `cost_usd`,
  `latency_ms`, optional `finish_reason`) and `RunRecord.turns: list[TurnRecord] =
  Field(default_factory=list)`. Each skill call already carries `step`; we associate
  it to its turn (add `turn: int` to `SkillCallRecord`, or derive from `step`).
- `Metrics` keeps its shape; `cost_usd` becomes the **sum** of turn costs + skill
  costs (today it effectively reflects the final completion only — see D5).

These are JSON-array fields alongside the existing `skill_calls` array — **no schema
migration** beyond an additive column for `turns` (note for `SqliteProvider` /
`PostgresProvider`). Old records (no `turns`) are valid and render as a single node.

### D3 — CLI tree rendering
`mdk explain` renders the retained model as a Rich `Tree`: run → `turn[i]` (model ·
tokens · cost · latency) → `skill`/`retrieval` children (name · cost · latency ·
ok/err). The existing flat skill table stays as the default and `--json` stays
byte-stable (additive keys only). Surface choice: promote the tree under `--steps`
(or add `--tree`) — decided at impl time; the flat table remains for narrow
terminals/scripts. `mdk trace replay` renders the same tree for the run it replays.
Remote runs compose for free: `mdk runs show <id> --target` already fetches the
`RunRecord` (and #125 fixed the hint to point there), so the tree renders from the
fetched record with no runtime API change.

### D4 — Workflow trace correlation
Build on ADR 019 (context already crosses the queue). The workflow orchestrator
opens a `workflow.execute` root span and passes its `SpanCtx` down so each node's
`agent.execute` starts with `parent=<workflow root>` — making the whole workflow
**one** trace tree in Langfuse/App Insights. Offline, `mdk explain` (and a future
`mdk workflow explain`) reconstruct the node tree from `RunRecord.workflow_run_id` +
`node_id`, which already exist. No new linkage field is needed.

### D5 — Cost attribution becomes a true sum (metrics-semantics refinement)
Per-turn cost is computed where each completion happens (the Executor already
computes `cost` for the completion at `executor.py:674-677`; extend to **per turn**
inside the loop). Skill cost = `cost.per_call_usd` per call (ADR 002). Run-level
`Metrics.cost_usd` = Σ(turn costs) + Σ(skill costs). For single-turn, no-skill
agents this equals today's value; for multi-turn / tool-using runs it becomes a
**more accurate, larger** number than today's effective final-completion figure.
This is a value-semantics change to an existing field (CLAUDE.md compat rule 5): it
ships with a CHANGELOG note and is called out in `mdk explain`; the field **shape**
is unchanged, so `--json` consumers and budget enforcement keep working.

### D6 — Scope / sequencing (this ADR decides; impl is sequenced PRs)
- **PR 1 (#100 + #99 backbone):** `TurnRecord` + `SkillCallRecord.cost_usd` + the
  nested child spans in the Executor. Retention (D2) and emission (D1) share the same
  loop instrumentation, so they land together. Includes the D5 sum + CHANGELOG.
- **PR 2 (#101):** CLI tree rendering in `mdk explain` / `mdk trace replay`, reading
  the retained `turns`/`skill_calls`.
- **PR 3 (#99 workflow half):** workflow-root → node span nesting (D4) + offline
  workflow tree.
PR 1 unblocks PR 2 and PR 3; each is independently shippable and testable.

## Consequences

**Positive**
- "Where did the cost/time/tokens go?" is answerable per step, in the trace UI **and**
  offline in `mdk explain` — no Langfuse dependency for the breakdown.
- One coherent step model across emission, retention, and rendering — no divergent
  shapes; the #120-class "two paths drift" failure is avoided by construction.
- Reuses the existing Tracer seam (`parent`) and existing JSON record columns — small
  surface, no Protocol change, no real migration, no boundary violations.
- Composes with remote runs (`mdk runs show`) and with ADR 023's retrieval step.

**Negative / risks**
- Touches `core/executor.py`, the hottest path. Mitigated: child spans are cheap/no-op
  when tracing is off; retention is plain field population; behavior of the non-RAG,
  single-turn path is unchanged. Regression-guarded by the test matrix.
- `Metrics.cost_usd` value changes for multi-turn/tool runs (D5) — a real, if
  additive-shaped, compat note. Mitigated by CHANGELOG + `explain` surfacing.
- `RunRecord` grows (`turns` + per-skill `cost_usd`); larger rows for tool-heavy runs.
  Bounded by the existing `max_tool_turns` cap (ADR 002) and acceptable for the
  observability value.
- Two `RunRecord` versions in storage (old without `turns`); handled by `default_factory`
  + a single-node render fallback.

**Test matrix (impl must cover):**
single-turn no-skill run → one turn node, `Metrics.cost_usd` unchanged (regression);
multi-turn tool-using run → per-turn + per-skill cost retained and summed correctly;
retrieval step (ADR 023 + model-driven) appears as a `retrieval.*` node with cost;
tracing OFF (NullTracer) → no spans emitted but `mdk explain` tree still renders from
the record (offline-first guard); old `RunRecord` with no `turns` → renders as a
single node, no crash; error mid-loop → partial turns/skills persisted, tree shows the
failure point; workflow run → node spans nest under the workflow root (D4) and the
offline node tree reconstructs from `workflow_run_id`/`node_id`.

## Alternatives considered

- **(a) Langfuse-only (emit nested spans, don't retain in the record).** *Rejected:*
  `mdk explain` must work offline (the whole reason `skill_calls` is persisted today),
  and Langfuse is an optional deployment choice (ADR 015). A breakdown that vanishes
  without a backend isn't acceptable.
- **(b) Add turn/skill/retrieval methods to the Tracer Protocol.** *Rejected:* the
  `parent` argument already enables nesting; new methods bloat a seam every backend
  (langfuse/otel/composite/null/stdout) must implement, for no gain.
- **(c) Reconstruct the tree at render time from flat `log_event`s.** *Rejected:*
  events aren't persisted in the record, are backend-specific, and reconstruction is
  fragile. The model belongs in the record (D2).
- **(d) Per-step flat table only (no tree), persisted.** *Rejected as the end-state:*
  #101 explicitly wants a tree (turn → tool/retrieval nesting is the legibility win);
  the flat table is kept as the fallback/`--json`, not the headline.
- **(e) A separate per-step metrics store / new table.** *Rejected:* over-engineered;
  the existing `RunRecord` JSON columns hold this cleanly, and `mdk explain` already
  loads the record. Revisit only if per-step analytics across runs needs its own query
  surface.
