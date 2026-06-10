# ADR 098 — Exclusive convergence (OR-merge): shared sinks for mutually exclusive branches

Status: Accepted
Date: 2026-06-09
Accepted: 2026-06-09 — approved by Jeremy. Ratified incl. clause (b) exclusive tails + bundled on_timeout validation fix.
Deciders: Engineering — validator-only relaxation behind the existing
`validate_linear` phase gate (CLAUDE.md §6/§7: validators are layered on the IR;
the IR and both runtimes are untouched).
Builds on: ADR 054/055 (Temporal compiler + native runner parity, the
`_sequential_successor` traversal contract), ADR 056 (judge branch legs),
ADR 062 (durable HITL timeout routes), ADR 092 (parallel fan-out/fan-in — the
*barrier* join this ADR is explicitly NOT), ADR 094 (decision node; closed #47
and named this gap #49).

## Context

`validate_linear` (`src/movate/core/workflow/compiler.py:558-563`) rejects any
node with more than one predecessor:

```python
joining = sorted(nid for nid in graph.nodes if len(graph.predecessors(nid)) > 1)
if joining:
    raise WorkflowCompileError(
        f"v0.3 forbids joins (>1 predecessor); offenders: {', '.join(joining)}. "
        f"Parallel fan-in lands in v1.1+."
    )
```

That guard predates every branching primitive we have since shipped. Today a
workflow can fork — `intent-router`, `judge`, and `decision` nodes are exempt
from the branch guard (`_branch_types`, compiler.py:546-557) and their route
legs are injected as synthetic `CONDITIONAL` edges (router compiler.py:345-362,
judge :369-387, decision :392-408) — but the branches can never **reconverge**.
Every fork is forced to carry its own private copy of the shared tail.

The certification suite made the cost concrete. The Expense Approval scenario
(`certification/scenarios/expense-approval/workflows/expense/workflow.yaml`)
has three approval tiers (auto / manager / director) that all end the same way:
post to the ERP, then finalize. Because the three tiers cannot share one sink,
the workflow ships **three copies of `erp-poster`** (`post-erp-auto`,
`post-erp-mgr`, `post-erp-dir`), **three copies of `finalize`**, and **two
copies of `rejected`** — 13 nodes for a process with 8 distinct steps. The
workflow's own description records this as authoring gap **#49**. The same
shape recurs in every approve/reject-tiered template (ITSM change approval,
refund approval): N tiers × M shared-tail steps of pure duplication, with the
usual drift failure mode (a fix lands on `post-erp-mgr` but not
`post-erp-dir`).

Crucially, the thing being asked for is **not** ADR 092's join. A
`PARALLEL_FAN_IN` join is a **barrier**: N branches all execute concurrently
and the join waits for all of them, merging state by a declared strategy
(`join: last_wins | by_key | collect`, compiler.py:328-331). Branches hanging
off a router/judge/decision (or a HUMAN gate's timeout route) are **mutually
exclusive** — exactly one executes per run. Several exclusive branches pointing
at one shared continuation is an **OR-merge**: no waiting, no state merge,
nothing to synchronize. The current guard rejects it only because it counts
predecessors, not because anything downstream would misbehave.

### Why the runtime already supports it (verified)

Both execution backends traverse by following a **single active cursor** and
never consult a node's in-degree:

- **Native runner** (`src/movate/core/workflow/runner.py`): `_walk_traced`
  (runner.py:364-663) holds one `current_id` and advances it per node-type:
  routers/judges/decisions return exactly one chosen target from node
  *metadata* (runner.py:414, :457, :465), and agent/human/supervisor nodes
  advance via `_sequential_successor` (runner.py:687-698), which reads only
  **outbound** edges and filters `synthetic` ones (runner.py:697). The HITL
  resume path uses the same helper (runner.py:254). The only place the runner
  reads `graph.predecessors(...)` at runtime is the fan-in merge of a parallel
  block (runner.py:873) — unreachable in a graph with no `PARALLEL_FAN_IN`
  edge. A node's predecessor count is therefore *invisible* to native
  execution.
- **Temporal compiler** (`src/movate/core/workflow/compilers/temporal.py`):
  the generated workflow is a dispatch loop with **one `if/elif current ==
  <id>:` arm per node** (temporal.py:529-541) — emitted per node, never per
  inbound edge. Advancement is `current = <next>` computed from successors
  only: agents via the same synthetic-filtering `_sequential_successor`
  (temporal.py:1093-1104), router gates via `current =
  <method>_routes.get(label, fallback)` (temporal.py:859), decisions via
  `current = evaluate_decision(...)` (temporal.py:892), HUMAN timeout routes
  via `current = <on_timeout>` (temporal.py:1073). Two dispatch arms assigning
  the same target id is *already* how two router routes share a target today;
  in-degree never appears in codegen.
- **LangGraph export** (`src/movate/core/workflow/compilers/langgraph.py`):
  edges are emitted per **source** node — `add_conditional_edges` per node
  with conditional out-edges, `add_edge` per sequential edge
  (langgraph.py:348-373). A shared target with several inbound conditional
  legs is idiomatic LangGraph (only the taken leg triggers the node).

So a shared sink with multiple inbound branch legs is already safe on every
backend. What *would* break is a node whose multiple inbound edges can be
active **in the same run** — i.e. predecessors that both execute — without
barrier semantics. In a `validate_linear`-shaped graph that situation is
structurally impossible (proof in D1): it requires parallel fan-out, and the
edge-kind guard (compiler.py:532-542) admits only `SEQUENTIAL` and synthetic
`CONDITIONAL` edges into this validator in the first place.

## Decision

Allow **exclusive convergence (OR-merge)** in the linear/router phase gate: a
node may have more than one predecessor when every inbound edge is a leg of a
mutually exclusive branch. `PARALLEL_FAN_IN` (ADR 092) remains the only
*barrier* join. This is a **validator-only** change.

### D1 — The validator rule: per-edge admissibility, exclusivity by construction

Replace the unconditional join guard in `validate_linear`
(compiler.py:558-563) with a per-join check. A node `j` with
`len(graph.predecessors(j)) > 1` is legal iff **every** inbound edge `e`
satisfies one of:

- **(a) routing leg** — `e.kind is EdgeKind.CONDITIONAL and
  e.metadata.get("synthetic")`: a compiler-injected branch leg from an
  `intent-router` / `judge` / `decision` (sources `"intent-router"`,
  `"judge"`, `"decision"`) or a HUMAN timeout route (`"human-timeout"`, new —
  see below); or
- **(b) exclusive tail** — `e.kind is EdgeKind.SEQUENTIAL`, non-synthetic, and
  its source's **non-synthetic out-degree is exactly 1** (this edge is the
  source's only real successor).

Anything else — a `PARALLEL_FAN_IN`/`PARALLEL_FAN_OUT` inbound (cannot occur
here: `declares_parallel` routes such graphs to `validate_dag`,
compiler.py:605-627), or a future non-synthetic `CONDITIONAL` — keeps failing,
with the error message updated to distinguish "needs barrier semantics — use
`fan_in` (ADR 092)" from the old blanket "v0.3 forbids joins".

**Why (a)+(b) is exactly "mutually exclusive":** under `validate_linear`'s
*other* guards, two predecessors of `j` can never both execute in one run.
Both executing requires the single active path to fork and both fork arms to
run — and the only constructs that put more than one successor on a node are
the routing primitives (the branch guard rejects >1 successor on anything
else, compiler.py:546-557), which activate **exactly one** leg per visit
(native: one chosen target returned, runner.py:414/:457/:465; Temporal: one
`current =` assignment, temporal.py:859/:892/:1073). Parallel edges — the only
way to have two simultaneously active cursors — are rejected by the edge-kind
guard (compiler.py:532-542) before the join check runs. Clause (b)'s
out-degree-1 condition is implied by the branch guard; checking it locally
makes the join rule self-contained rather than dependent on guard ordering.
The acyclicity check (compiler.py:444-448) rules out re-entering `j` through
the second edge later in the same run. So exclusivity is **structural**, not
inferred — the validator needs no path/dominator analysis.

Clause (b) is what lets branches converge *after* per-branch work, not just
directly at the router's target: `manager-decision →(approve) notify-mgr →
post-erp` and `director-decision →(approve) notify-dir → post-erp` is legal —
`post-erp` has two non-synthetic `SEQUENTIAL` inbound edges, each its source's
only real successor.

**HUMAN timeout routes become first-class branch legs.** Today
`compile_workflow` stamps `on_timeout` into node metadata only
(compiler.py:144); the target id is **never validated** (a typo fails at run
time as the Temporal dispatch loop's "unknown workflow node",
temporal.py:547-550) and **no edge is injected**, so a timeout-only
continuation node would fail the reachability check. As part of this ADR,
`compile_workflow` (i) validates the `on_timeout` target id like router/judge/
decision targets (steps 3/3b/3c, compiler.py:255-295) and (ii) injects a
synthetic `CONDITIONAL` edge `{"synthetic": True, "source": "human-timeout"}`,
making a HUMAN gate's timeout leg convergence-eligible under clause (a).
Survey: no in-tree workflow or test points `on_timeout` backwards (all targets
are forward and already reachable — e.g. tests/test_temporal_execution.py:855),
so the new edge cannot trip the acyclicity check for any existing spec; a
deliberate backwards re-notify loop remains the `allow_cycles` export path's
business.

### D2 — Zero runtime change, on both backends

No execution code changes. The evidence (file:line, verified):

| Surface | Advance mechanism | In-degree consulted? |
|---|---|---|
| Native walk | `_sequential_successor` outbound-only, synthetic-filtered (runner.py:687-698); routers/judges/decisions return one target from metadata (runner.py:414/:457/:465) | Never (only `PARALLEL_FAN_IN` merge, runner.py:873) |
| Native HITL resume | same helper (runner.py:254) | Never |
| Temporal codegen | one dispatch arm per node (temporal.py:529-541); `current = <next>` from successors only (temporal.py:859/:892/:1062/:1073/:1093-1104) | Never |
| LangGraph export | per-source `add_edge`/`add_conditional_edges` (langgraph.py:348-373) | Never |

A shared target reached by two router routes already exercises this machinery
today (two arms assigning the same `current`); the relaxation only lets the
two arms belong to *different* routers. The only code change outside the
validator is the **compile-time** human-timeout edge injection in D1 — also
not runtime. Conformance tests (the ADR 055 D7 pattern) will assert native ≡
Temporal final state for a converged workflow as the proof-in-CI.

### D3 — Division of labor with ADR 092: OR-merge vs barrier join

`PARALLEL_FAN_IN` stays the one and only **barrier**: all branches execute,
the join waits and merges state by the declared strategy. OR-merge is
exclusively for **conditional** branches: one branch executes, nothing waits,
no merge strategy exists or is accepted. The validators tell them apart by
**edge kind, at the dispatch seam**:

- `validate_graph` (compiler.py:616-627) routes any graph declaring a
  `PARALLEL_*` edge to `validate_dag`; everything else takes
  `validate_linear`. OR-merge therefore lives only in the `validate_linear`
  path; barrier joins only in the `validate_dag` path. Today they cannot even
  coexist in one graph (`validate_dag` Phase 1 is agent-only,
  compiler.py:651-657).
- Per node, the rule is kind-homogeneity — `validate_dag` already rejects a
  join that "mixes fan-in with other edge kinds" (compiler.py:686-692); the
  OR-merge rule (D1) symmetrically admits only conditional/sequential inbound.
  When routers inside parallel blocks land (ADR 092 later phase), this
  per-node homogeneity rule is the forward-compatible discriminator: all
  inbound `fan_in` ⇒ barrier; all inbound exclusive legs ⇒ OR-merge; mixed ⇒
  error.
- A `join:`/`join_key:` strategy on a non-`fan_in` edge stays rejected by the
  spec validator (fan-in-only, compiler.py:326-331) — authors cannot ask an
  OR-merge to merge.

### D4 — Compatibility: purely additive

The relaxation only admits graphs that previously **failed** with "v0.3
forbids joins (>1 predecessor)". Any workflow that validates today has no node
with >1 predecessor, so the new per-join check never executes for it —
acceptance is identical, error behavior elsewhere is identical. The branch
guard, edge-kind guard, source guard, multi-sink relaxation
(compiler.py:581-592), and `validate_dag` are all untouched. The
human-timeout synthetic edge adds IR edges only for HUMAN nodes that declare
`timeout` (today: forward targets only, so no acyclicity regressions); its
only observable effects are positive (compile-time typo detection,
reachability for timeout-only continuations) plus a cosmetic edge-count/topo
change in `mdk validate` output. No change to `agent.yaml`/`project.yaml`
schema, public CLI flags or `--json` shapes, `/api/v1`, storage schema,
`MOVATE_*`/`MDK_*` env vars, or deploy behavior. CalVer is git-derived; no
version line.

## Alternatives considered

- **Status quo — keep duplicating the shared tail.** Free today, but the cost
  is per-workflow and permanent: N tiers × M shared steps of copies, drift
  risk (the fix that lands on one copy), noisier traces/evals (three
  `erp-poster` nodes to monitor instead of one), and the certification suite's
  top remaining authoring-tax finding. Rejected: the validator is rejecting a
  shape both runtimes already execute correctly — pure authoring tax.
- **Replace `validate_linear` with a general DAG validator.** One validator,
  no special cases — but it abandons the phase-gate firewall deliberately
  (validators are the layered, replaceable seam; ADR 092 D1 kept
  `validate_linear` byte-for-byte for exactly this reason), and a general
  validator must then *re-derive* which joins need barriers vs not — the
  analysis D1 gets structurally. It would also silently legalize shapes whose
  runtime semantics are ambiguous today (e.g. >1 non-synthetic sequential
  successor, where `_sequential_successor` silently takes `seq[0]`,
  runner.py:698). Rejected for blast radius; the OR-merge rule is the entire
  semantic content such a validator would add for conditional graphs.
- **Sub-workflow extraction.** Model the shared tail as a `SUB_WORKFLOW`
  invoked from each branch. `NodeType.SUB_WORKFLOW` is an unimplemented IR
  placeholder (ir.py:34, "v1.2"), so this trades a ~30-line validator change
  for a whole new execution primitive on two backends — and still leaves the
  graph-shape restriction in place (each branch still needs its own caller
  node). Worth doing eventually for *large* reusable tails; not a substitute
  for convergence.

## Consequences

- **Expense Approval** (`certification/scenarios/expense-approval/`): 13 nodes
  → **8** (classify, manager-approval, manager-decision, director-approval,
  director-decision, shared `post-erp`, `finalize`, `rejected`); explicit
  edges 5 → 3; `erp-poster` ×3 → ×1, `finalize` ×3 → ×1, `rejected` ×2 → ×1.
  (9 nodes if a deliverable keeps per-tier `rejected` messaging.) Gap #49
  closes; the scenario's remaining authoring tax is #48 alone (HUMAN nodes
  routing through an LLM intent-router on approve/reject).
- Every tiered approve/reject template (ITSM change approval,
  `workflows/refund-approval/`-style flows) gets the same dedup: one ERP/
  notify/finalize tail regardless of tier count; one node to fix, trace, and
  eval instead of N.
- The mental model sharpens to: **branch with a routing primitive, converge
  freely, synchronize only with `fan_in`** — and the validators enforce each
  clause separately.
- Implementation scope (one PR): the `validate_linear` join-guard replacement
  (~30 lines), `compile_workflow` on_timeout validation + synthetic-edge
  injection (~25 lines), tests (accept/reject matrix incl. the
  mixed/parallel-inbound rejections; native+Temporal conformance over a
  converged workflow; LangGraph export smoke; on_timeout typo), certification
  scenario + USER_GUIDE updates. No `src/` runtime files change.

## Verification

```
ruff check src tests && ruff format --check src tests
mypy src
pytest -m "not smoke" tests/test_workflow*.py tests/test_temporal_*.py
pytest -m "not smoke"            # every existing workflow validates identically
```

- A converged (shared-sink) router workflow validates, runs on native, and
  compiles + runs on Temporal with identical final state.
- Every pre-ADR workflow in `tests/`, `workflows/`, and templates validates
  with byte-identical results; the "forbids joins" error still fires for
  barrier-needing shapes, now pointing at `fan_in`.
