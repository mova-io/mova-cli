# LangGraph seam — IR findings and v1.1 design plan

**Status:** linear AGENT case shipped behind `workflow.yaml: runtime:
langgraph` (see [`src/movate/core/workflow/compilers/langgraph.py`](../src/movate/core/workflow/compilers/langgraph.py)).
Sections below describe the **next** v1.1 additions — conditional /
parallel / HITL — that the prototype foreshadowed.

## Why this doc exists

The [implementation roadmap](../../.claude/plans/want-to-take-inspiration-stateful-swan.md)
called out **workflow-IR design lock-in** as the #1 risk for v1.1:

> A bad IR makes Phase 7's LangGraph swap-in painful. *Mitigation:* before
> writing the IR, sketch what LangGraph nodes / HITL / conditional /
> parallel constructs need; design IR to subsume them even though v0.3
> only emits linear DAGs. Build a throwaway IR→LangGraph prototype to
> validate the seam, then delete until v1.1.

The spike ran. The seam held. This doc captured the findings; the
linear case then shipped as production code at
[`src/movate/core/workflow/compilers/langgraph.py`](../src/movate/core/workflow/compilers/langgraph.py).
The remaining v1.1 work below extends that compiler in additive
strokes.

## TL;DR

**The v0.3 IR is fundamentally sound.** Every v1.1 LangGraph construct
maps onto the existing `NodeType` / `EdgeKind` enums and `WorkflowNode` /
`WorkflowEdge` dataclasses **without a breaking change**. The IR needs
only *additive* fields to be production-grade for v1.1.

We validated four mappings end-to-end:

| # | Mapping | Status | IR additions needed |
|---|---|---|---|
| 1 | Linear (v0.3) — `AGENT` + `SEQUENTIAL` | ✅ shipped (v1.0) | None |
| 2 | Conditional — `CONDITIONAL` edges | ✅ shipped (v1.1) | Condition DSL syntax + default-branch convention |
| 3 | Parallel — `PARALLEL_FAN_OUT/IN` | ✅ shipped (v1.1) | State-schema reducer annotations + node-output convention (deltas vs full) |
| 4 | HITL — `HUMAN` nodes | ✅ shipped (v1.1) | Resume-payload schema + checkpointer config |

No `NodeType` or `EdgeKind` variants need to be added or renamed. No
field on the existing dataclasses needs to change type or be removed.
Everything below is additive.

---

## What the prototype proved works

### 1. Linear (v0.3) — direct mapping

Trivial. `AGENT` node → `graph.add_node(id, runner_fn)`. `SEQUENTIAL` edge
→ `graph.add_edge(from, to)`. Source → `START`, sink → `END`.

The only consideration: LangGraph's `StateGraph(dict)` (untyped state)
replaces state on each step rather than shallow-merging. v0.3 doesn't hit
this because our homegrown runner merges explicitly; v1.1 should
materialise a `TypedDict` from the workflow's `state_schema` so
LangGraph's per-key shallow merge kicks in automatically.

### 2. Conditional (v1.1) — works with a router fn

`add_conditional_edges(source, router_fn, target_map)`. Multiple
`CONDITIONAL` edges out of one node compile into one router fn that
returns the first matching branch's target id.

### 3. Parallel (v1.1) — runs concurrently with a reducer

Multiple `PARALLEL_FAN_OUT` edges from one source → LangGraph runs them
in parallel by default. `PARALLEL_FAN_IN` is implicit (multiple edges
into one sink → LangGraph awaits all). Any state key written by parallel
branches needs an explicit reducer (e.g. `Annotated[list, operator.add]`)
or LangGraph raises `InvalidUpdateError`.

### 4. HITL (v1.1) — `interrupt_before` + checkpointer

`graph.compile(checkpointer=..., interrupt_before=[human_id_1, ...])`.
External system invokes with `config={"configurable": {"thread_id": ...}}`
to identify the run, calls `graph.update_state(config, {human_payload})`
to merge the response, then `graph.invoke(None, config)` to continue
from the checkpoint.

---

## Recommended IR additions for v1.1

Each below is **non-breaking** — add the field, default it to the v0.3
semantic, and the existing runner / compiler keep working.

### A. State-schema reducer annotations

**Problem:** `state_schema: dict[str, Any]` today is plain JSON Schema.
LangGraph needs per-key reducer hints for any field that parallel
branches write.

**Recommendation:** extend the schema with a `x-movate-reducer` annotation:

```yaml
state_schema:
  type: object
  properties:
    history:
      type: array
      items: {type: string}
      x-movate-reducer: append      # → operator.add
    seen_urls:
      type: array
      items: {type: string}
      x-movate-reducer: union       # → set-merge
    score:
      type: number
      x-movate-reducer: max         # → max()
```

Compiler maps named reducers to LangGraph-compatible callables. Keys
without an annotation default to "replace" (LangGraph's standard
shallow-merge). Reject unknown reducer names at compile time.

**Implementation note:** validate-time check that any key written by a
node downstream of a `PARALLEL_FAN_OUT` edge HAS a reducer. Without it,
the workflow silently picks one branch's value and drops the others.

### B. Conditional-edge DSL

**Problem:** `WorkflowEdge.condition: str | None` is unconstrained today.
The prototype used Python `eval` against state, which is a
code-injection vector in production.

**Recommendation:** define a small subset of JSONPath + comparison
operators for `condition`. Cheap to validate, no sandbox-escape surface,
matches operator expectations from YAML config.

```yaml
edges:
  - from: classify
    to: needs_review
    kind: conditional
    condition: "$.score < 0.7"
  - from: classify
    to: auto_approve
    kind: conditional
    condition: "$.score >= 0.7 && $.confidence > 0.9"
  - from: classify
    to: fallback
    kind: conditional
    condition: null        # explicit "else" — see (C)
```

Parse at compile time into an AST. Evaluate against state at runtime.
Supported expressions:

- `$.<path>` — JSONPath against state
- `==`, `!=`, `<`, `<=`, `>`, `>=`
- `&&`, `||`, `!`
- string / number / boolean / null literals
- `in [...]` for set membership

Defer regex, arithmetic, function calls until a user asks.

### C. Default-branch convention for conditional fan-out

**Problem:** if no `CONDITIONAL` edge matches, the prototype raised. In
production we want a deterministic "else" path.

**Recommendation:** require the LAST conditional edge from a node to
have `condition: null` (interpreted as "always matches"). Compiler
enforces this at validation: a node with conditional outbounds must have
exactly one null-condition edge, and it must be last in the `edges:`
list.

Alternative considered: add `WorkflowNode.default_target: str | None`.
Rejected because it duplicates information that's already in the edges
and creates an extra place for config drift.

### D. HUMAN node resume payload schema  ✅ shipped

**Problem:** `WorkflowNode.metadata: dict[str, Any]` is a stash; HUMAN
nodes need a typed contract for what the external resume payload looks
like.

**Shipped:** typed field on `WorkflowNode` and `NodeSpec`:

```python
@dataclass
class WorkflowNode:
    ...
    resume_payload_schema: dict[str, Any] | None = None
    """JSON Schema for the payload an external system must supply to
    resume a HUMAN node. Required when type is HUMAN; ignored otherwise.
    Validated at YAML parse time via Pydantic model_validator; resume.py
    validates the resume input against it (Draft 2020-12) before calling
    `graph.aupdate_state`."""
```

YAML-time validation: `type == HUMAN ⇒ resume_payload_schema is not None`
(otherwise `WorkflowSpecLoadError`). Runtime validation: bad payload →
`ResumeError` before LangGraph is even touched.

### E. Workflow-level checkpointer config  ✅ shipped

**Shipped:** top-level workflow YAML field with a movate-provided enum:

```yaml
checkpointer: postgres     # one of: memory | sqlite | postgres
```

The compiler injects a `TenantNamespacedCheckpointer` wrapping LangGraph's
`BaseCheckpointSaver` at `graph.compile(checkpointer=...)`. Tenant
namespacing prefixes every `thread_id` with `tenant_id::` so cross-tenant
threads stay invisible. HUMAN-node workflows MUST declare a checkpointer
or compile fails with an actionable pointer.

SQLite persists at `~/.movate/checkpoints.db` (override via
`MOVATE_CHECKPOINT_DB`); Postgres uses `MOVATE_CHECKPOINT_PG_DSN` (or
`MOVATE_DB_URL` as fallback).

### F. Node output convention: deltas vs full state

**Problem (subtle):** for parallel branches with a reducer, nodes MUST
return deltas (just their contribution) — returning full state
double-counts the upstream state. For sequential nodes with no reducer,
returning either delta or full state works.

**Recommendation:** standardise on **deltas only** across all node types.
The runner wrapper around `Executor.execute()` projects state → input,
calls executor, returns ONLY the output keys. The state schema's
reducers (defaulting to "replace" for unannotated keys) handle merging.

This makes parallel nodes work without special casing, makes node
behaviour identical across topologies, and matches LangGraph's
documented best practice.

---

## Open questions for v1.1

1. **Workflow checkpoint isolation per tenant.** Multi-tenant safety:
   how do we ensure tenant A can't resume tenant B's HITL workflow? The
   answer is probably "checkpoint key includes `tenant_id` and resume
   API enforces match" — but worth designing before code lands.
2. **Resume idempotency.** What happens if the human submits the same
   resume payload twice? LangGraph's checkpoint logic should make this
   safe (the workflow is at a single point in the topology), but worth
   a test.
3. **Long-running HITL TTLs.** Postgres checkpoints accumulate. Need a
   sweep job that drops workflow runs that have been paused for > N
   days. New `WorkflowConfig.hitl_timeout` field.
4. **Sub-workflow state mapping.** SUB_WORKFLOW nodes (v1.2) need a
   declared projection: which parent-state keys feed into the child,
   and how does the child's output merge back. The IR doesn't have a
   field for this yet — defer to v1.2 design.
5. **Streaming output.** LangGraph supports streaming intermediate node
   outputs. Useful for interactive UIs at the v0.5 server layer. Not on
   the v1.1 critical path but consider whether `Executor` should expose
   an async-iterator variant in parallel with the existing
   request-response shape.

---

## What's already done (linear case + tenant-namespaced checkpointer)

- [x] `workflow.yaml: runtime: <homegrown | langgraph>` field. Default
      `homegrown`; opt-in to `langgraph` per workflow.
- [x] `src/movate/core/workflow/compilers/langgraph.py` — the linear
      compiler. AGENT nodes wrap `Executor.execute` so retry / fallback
      / cost / tracing / tenant isolation compose with LangGraph's
      node-fn lifecycle.
- [x] `WorkflowRunner.run` dispatches on `graph.runtime`. Same
      `WorkflowResult` shape under either path; callers don't branch.
- [x] Equivalence tests
      ([`tests/test_workflow_langgraph.py`](../tests/test_workflow_langgraph.py))
      cover happy path, partial-failure short-circuit, initial-state
      rejection, capability gate, and the missing-dep install-hint path.
- [x] Spike (`scripts/langgraph_prototype.py`) deleted.
- [x] **`workflow.yaml: checkpointer: <memory | sqlite | postgres>` field**
      (Tier 2 #2). `TenantNamespacedCheckpointer` wraps LangGraph's
      `BaseCheckpointSaver` so every checkpoint operation prefixes
      `thread_id` with `tenant_id::` — tenant A's workflow threads are
      invisible to tenant B even with colliding workflow_run_ids. **All
      three backends ship.** Memory via the sync `make_checkpointer`;
      SQLite + Postgres via the async-context-manager `async_checkpointer`
      which handles the connection-pool lifecycle around `ainvoke`.
      SQLite persists at `~/.movate/checkpoints.db` (override via
      `MOVATE_CHECKPOINT_DB`); Postgres uses `MOVATE_CHECKPOINT_PG_DSN`
      (or `MOVATE_DB_URL` as fallback). 15 tests in
      [`tests/test_workflow_checkpointer.py`](../tests/test_workflow_checkpointer.py).
- [x] **Conditional edges + JSONPath-like DSL** (Tier 2 #5). New
      `kind: conditional` / `when: <expr>` fields on `EdgeSpec`; YAML
      validator catches `kind: sequential` + `when:` at parse time.
      Hand-rolled recursive-descent parser at
      [`src/movate/core/workflow/condition_dsl.py`](../src/movate/core/workflow/condition_dsl.py)
      — no `eval`, no third-party dep. Supports comparisons (`==`,
      `!=`, `<`, `<=`, `>`, `>=`), boolean ops (`&&`, `||`, `!`),
      `in [...]` membership, nested JSONPath (`$.user.score`), short-
      circuit semantics, parentheses. New `validate_conditional` +
      `validate_for_runtime` dispatcher in the compiler so callers
      pick the right validator from `graph.runtime`. The langgraph
      compiler emits `add_conditional_edges` with a router fn that
      walks branches in YAML order; explicit `when: null` default
      required and enforced as last edge per source. 47 tests in
      [`tests/test_workflow_conditional.py`](../tests/test_workflow_conditional.py).
- [x] **Parallel fan-out + reducer annotations** (Tier 2 #6). New
      `kind: parallel_fan_out` / `parallel_fan_in` edge kinds on
      `EdgeSpec`. State-schema gains `x-movate-reducer: <name>` JSON
      Schema extension with six registered reducers (`append`,
      `union`, `max`, `min`, `last`, `merge`). Compiler detects
      parallel edges + materialises a `TypedDict` from `state_schema`
      via [`compilers/_typed_state.py`](../src/movate/core/workflow/compilers/_typed_state.py)
      so LangGraph's per-key shallow-merge / reducer-merge does the
      right thing. Node fns return delta-only when typed state is in
      play; full-state for the dict path (back-compat for non-parallel
      workflows). New `validate_dag` is the most permissive validator —
      accepts conditional + parallel with mixed-kinds detection and a
      minimum-2-branches rule for fan-outs. 28 tests in
      [`tests/test_workflow_parallel.py`](../tests/test_workflow_parallel.py).

## What to do at v1.1 (additive on the linear compiler)

1. Re-read this doc.
2. Add the additive IR fields described in §A–§F above.
3. Extend the existing compiler at
   `src/movate/core/workflow/compilers/langgraph.py`:
   - Loosen `can_compile`'s linear/AGENT-only assertion as each
     feature lands. Each rejection branch becomes a "compile this
     construct" branch.
   - Wire `add_conditional_edges` (§B). Plug a real expression sandbox
     in for `condition:` evaluation — do NOT use `eval`.
   - Materialise a `TypedDict` from `state_schema` with reducers from
     the `x-movate-reducer` annotation (§A). Switch parallel branches
     to delta-only returns.
   - Add `interrupt_before` for HUMAN nodes + checkpointer injection
     based on `WorkflowSpec.checkpointer` (§D-§E).
4. Add per-feature equivalence tests against the homegrown runner where
   the homegrown runner supports the construct, or against a frozen
   golden output where it doesn't.

## Provenance

- Built against LangGraph 1.1.10 (current as of 2026-05-11).
- LangGraph API has been stable since 0.2.x for the constructs we use
  (`StateGraph`, `add_conditional_edges`, `interrupt_before`,
  `update_state`, `MemorySaver`). A 2.0 release would warrant a
  re-validation pass through the shipped compiler + this doc.
- Production compiler: [`src/movate/core/workflow/compilers/langgraph.py`](../src/movate/core/workflow/compilers/langgraph.py).
- Equivalence tests: [`tests/test_workflow_langgraph.py`](../tests/test_workflow_langgraph.py).
