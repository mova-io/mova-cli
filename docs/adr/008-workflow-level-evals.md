# ADR 008 — Workflow-level evals

**Status:** Proposed
**Date:** 2026-05-16
**Deciders:** Engineering
**Context window:** v0.8 (post-GA polish, Item 16)
**Supersedes:** N/A
**Related:** [ADR 002 — Skills and contexts](002-skills-and-contexts.md) for the
agent-bundle eval precedent; `src/movate/core/eval.py` (`EvalEngine`);
`src/movate/core/workflow/runner.py` (`WorkflowRunner`, `WorkflowResult`)

---

## Decision

Extend `mdk eval` to evaluate multi-node workflows end-to-end by:

1. **Detecting workflow paths** via the existing `is_workflow_path()` helper and
   routing to a new `WorkflowEvalEngine` instead of the existing `EvalEngine`.
2. **Scoring against `final_state`** — the merged output dict after the sink
   node runs — not per-node outputs.
3. **Placing the dataset at `evals/dataset.jsonl`** inside the workflow
   directory, mirroring the agent-bundle convention exactly.
4. **Extending `WorkflowSpec`** with an optional `evals:` stanza so CI gates
   (accuracy threshold, runs per case) can live in the same file as the
   workflow graph.
5. **Running the full workflow graph N times** per eval case (not N times
   per node) so multi-run statistics cover the workflow as an atomic unit.

In one sentence: **"a workflow eval is a full `WorkflowRunner.run()` call per
case, scored on `final_state`, with the same dimensional scoring machinery the
agent eval engine already has."**

---

## Context

`mdk eval <agent-dir>` is the established eval surface. It loads an
`AgentBundle`, runs the `EvalEngine`, and scores each case on accuracy,
faithfulness, coverage, latency, and (optionally) refusal. The CLI already
uses `is_workflow_path()` to detect `workflow.yaml` files, but today that
branch only errors with "workflow evals not yet supported."

Operators who model a ticket-routing pipeline as two sequential agents —
`triage → draft-reply` — have no way to verify the end-to-end output. They
must eval each agent in isolation, which misses:

* **State threading bugs** — the triage agent's `routing_queue` is correct,
  but the draft-reply agent silently ignores it and drafts for the wrong queue.
* **Latency accumulation** — the workflow is fast on paper, but the sum of
  node latencies exceeds the SLA.
* **Refusal propagation** — a harmful request that the triage agent
  classifies as `p0_urgent` still reaches the draft-reply node and produces a
  compliant response when `refusal_expected=true` for the whole pipeline.

Item 16 in the polish plan calls for workflow-level evals after an ADR is
accepted. This is that ADR.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Minimal new surface** — reuse `EvalCase`, `DimensionScores`, `_check_dimensional_gates`, `--gate-*` flags as-is | HIGH |
| **Operator ergonomics** — `mdk eval <workflow-dir>` should "just work" the same as `mdk eval <agent-dir>` | HIGH |
| **Score what matters** — final output, not every intermediate node | HIGH |
| **Testability** — `WorkflowEvalEngine` must accept an injected `WorkflowRunner` so tests never hit real providers | HIGH |
| **Backward compatibility** — existing agent evals must be unaffected | HIGH |
| **Per-node visibility** — partial failure (node N crashes) must surface clearly, not swallow into a generic error | MED |

---

## Architecture

```
mdk eval <workflow-dir>
         │
         ├─ is_workflow_path(path) → True
         │
         ▼
  WorkflowEvalEngine.run(bundle)
         │
         │  for each EvalCase in dataset:
         │    for run in range(runs_per_case):
         │      WorkflowRunner.run(graph, initial_state=case.input)
         │        → WorkflowResult(final_state, runs[], status)
         │      score(final_state, case.expected) → DimensionScores
         │
         ▼
  EvalSummary  (same type as agent eval)
         │
         ▼
  _emit_dimensional_breakdown()  (same function, no changes)
  _check_dimensional_gates()     (same function, no changes)
```

The CLI's `_run_eval()` function already dispatches on the path type.
`WorkflowEvalEngine` returns an `EvalSummary`, so all downstream display and
gate-checking code is shared with agent evals.

---

## Decisions

### Decision 1: Dataset at `evals/dataset.jsonl` inside the workflow directory

The workflow directory already contains `workflow.yaml`, `state_schema.json`,
and (optionally) `evals/`. Placing the dataset there mirrors the agent-bundle
layout exactly:

```
my-pipeline/
├── workflow.yaml
├── state_schema.json
└── evals/
    └── dataset.jsonl
```

Each `dataset.jsonl` row must follow the existing `EvalCase` schema:
`input` maps to `initial_state`; `expected` maps to the keys the scorer
checks in `final_state`.

**Why not a shared `evals/` at the project root?** Workflows are independently
versioned and deployed. A shared dataset becomes an implicit coupling point
between pipelines. Keeping the dataset adjacent to `workflow.yaml` means the
whole workflow eval is self-contained for `git archive` and CI artifact
uploading.

**Why not a new `WorkflowEvalCase`?** `EvalCase` already supports every
field we need (`input`, `expected`, `grounding`, `expected_coverage`,
`refusal_expected`, `skill_responses`). Adding a parallel class creates
maintenance debt with no benefit.

### Decision 2: Score `final_state`, not per-node outputs

`WorkflowResult.final_state` is the state dict after the sink node's output
has been merged in. Accuracy scoring compares `final_state` against
`case.expected` using the same partial-match logic that agents use today
(keys present in `expected` are checked; extra keys in `final_state` are
ignored).

**Why not score each node?** The eval system answers "does the workflow
produce the right output?" — a product question. Scoring every intermediate
node turns that into "is each agent correct in isolation?" — a unit-test
question answered better by running `mdk eval` on each agent directory.
Workflow-level evals occupy the integration layer; per-node evals occupy the
unit layer.

**Why not a weighted average of per-node scores?** The weighting would be
arbitrary. Intermediate nodes may output fields that are internal plumbing
(routing hints, confidence values) that have no ground truth in the eval
dataset.

### Decision 3: New `WorkflowEvalEngine`, not adapting `EvalEngine`

`EvalEngine` is parametrized on `AgentBundle` and calls `executor.execute(bundle, ...)`.
`WorkflowEvalEngine` is parametrized on `WorkflowGraph` and calls
`runner.run(graph, initial_state)`. The two types are not structurally
compatible at the call-site level — adapting `EvalEngine` would require
threading a union type through a dozen private methods and making the
`runner` / `bundle` distinction implicit.

`WorkflowEvalEngine` instead:

* Accepts `executor: Executor` and `storage: StorageProvider` (same deps).
* Constructs a `WorkflowRunner` internally, or accepts an injected one for
  testing.
* Contains its own `_score_case()` method that calls the shared
  `_score_dimensions()` free function from `eval.py` — dimensional scoring
  is reused verbatim.
* Returns `EvalSummary` — same type — so the CLI display layer is unchanged.

**Shared free functions (no changes needed):**

| Function | Reused as-is |
|---|---|
| `_score_accuracy()` | ✓ |
| `_score_faithfulness()` | ✓ |
| `_score_coverage()` | ✓ |
| `_score_refusal()` | ✓ |
| `_score_context_compliance()` | ✓ |
| `_compute_dimensional_means()` | ✓ |
| `load_dataset()` | ✓ |

### Decision 4: Latency = total workflow duration

`WorkflowResult.duration_ms` already accumulates wall-clock time across all
nodes (including executor overhead). The latency dimension uses this value
directly. The per-node `RunRecord.metrics.latency_ms` values are available
in `WorkflowResult.runs` for diagnostic purposes but are not part of the
dimensional score.

### Decision 5: N runs = N full workflow executions

`runs_per_case=3` means `WorkflowRunner.run()` is called 3 times for each
`EvalCase`. The 3 resulting `WorkflowResult` objects are aggregated exactly
as `EvalEngine` aggregates `N` single-agent runs. This matches operator
intuition: "run the whole pipeline 3 times and average the scores."

**Why not N runs per node?** If the workflow has 4 nodes and `runs_per_case=3`,
"3 runs per node" would mean 12 model calls. Operators who set `--gate 0.9`
expect the gate to reflect full pipeline reliability, not per-node reliability
averaged. The per-node retry budget (`retries:` in `agent.yaml`) handles
within-run robustness separately.

### Decision 6: Partial node failure → case scored as failed run

When `WorkflowResult.status == ERROR`, the `WorkflowEvalEngine` records the
run with `score=0.0`, `rationale="workflow stopped at node <id>: <error>"`,
and dimension scores all `None` (dimensions are not meaningful for a partial
run). The case's `aggregated_score` is the mean of all N runs, so one crashed
run lowers the score but does not make the case unmeasurable.

**Why not abort the eval on first crash?** A crash in one run may be a
flaky provider timeout. With `runs_per_case > 1`, the eval surfaces "flaky
2/3 of the time" as a 0.33 score rather than an exception. The operator
decides whether `--gate 0.8` is acceptable.

### Decision 7: `skill_responses` applies across all nodes by skill name

`EvalCase.skill_responses` is a dict keyed by skill name. In a multi-node
workflow where two different agents both call the `kb-lookup` skill,
`skill_responses["kb-lookup"]` applies to both nodes. If a node's skill has
a name collision with another node's skill, the same stub response is returned
to both — this is the desired behavior for the common case (shared skills
should behave consistently across nodes).

When node-specific skill fixtures are required (future need), a
`node_skill_responses: dict[node_id, dict[skill_name, ...]]` field can be
added to `EvalCase` without breaking existing datasets.

### Decision 8: `WorkflowSpec` gains an optional `evals:` stanza

```yaml
# workflow.yaml
api_version: movate/v1
kind: Workflow
name: ticket-pipeline
version: 0.1.0
state_schema: state_schema.json
entrypoint: triage

evals:
  dataset: evals/dataset.jsonl  # relative to workflow.yaml
  runs_per_case: 3
  gate: 0.8                     # accuracy gate default

nodes: ...
edges: ...
```

The stanza mirrors `AgentSpec.evals`. All fields are optional — omitting
`evals:` means the workflow is not eval-able until the operator adds one.
`mdk validate` gains a check that warns when a workflow has no evals stanza.

---

## What goes where

| Concern | Location |
|---|---|
| **Engine** | `src/movate/core/eval.py` — new `WorkflowEvalEngine` class |
| **`WorkflowSpec.evals` stanza** | `src/movate/core/workflow/spec.py` — new optional `WorkflowEvalsSpec` model |
| **CLI dispatch** | `src/movate/cli/eval.py` — `_run_eval()` branches on `is_workflow_path(path)` |
| **Dataset** | `<workflow-dir>/evals/dataset.jsonl` |
| **Tests** | `tests/test_eval_workflow.py` — new; mirrors `test_eval_refusal_dim.py` structure |

---

## Phasing

**Phase 1 — Engine + dataset loading (Item 16a):**

* `WorkflowEvalsSpec` Pydantic model + `WorkflowSpec.evals` field
* `load_workflow_dataset()` free function (thin wrapper on `load_dataset`)
* `WorkflowEvalEngine.run()` — accuracy-only scoring of `final_state`
* `mdk eval <workflow-dir>` dispatch in `_run_eval()`
* Tests: engine unit tests with mock `WorkflowRunner`, CLI integration test
  with `ticket-pipeline` scaffold

**Phase 2 — Dimensional scoring + gates (Item 16b):**

* `grounding`, `expected_coverage`, `refusal_expected` in workflow dataset
  rows — all map to the same `EvalCase` fields, no new code
* `--gate-faithfulness`, `--gate-coverage`, `--gate-refusal` flags apply
  to workflow evals automatically (shared `_check_dimensional_gates`)
* `mdk validate` adds warning when `workflow.yaml` has no `evals:` stanza

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Provider cost** — N runs × K nodes is expensive for large workflows | Document clearly; `--mock` flag works identically to agent evals |
| **State schema drift** — `dataset.jsonl` rows use a stale `initial_state` shape** | `load_workflow_dataset()` validates each row against `state_schema.json` via `jsonschema`; mismatch → `EvalConfigError` |
| **Flaky per-node skills cause misleading accuracy scores** | `skill_responses` stubs are the recommended path for deterministic eval; document in `mdk eval --help` output |
| **Workflow graph changes break existing dataset** | `mdk validate` warns when an `EvalCase.input` key is not in `state_schema`; breaking changes require a dataset migration |
| **`final_state` has more keys than `expected`** | Already handled: accuracy scoring checks only keys present in `expected` (partial match) |

---

## Open questions

1. **Fan-out nodes (v1.1)** — when the workflow spec adds parallel branching,
   `final_state` is no longer well-defined (merge conflicts, ordering). Defer
   to a follow-up ADR when parallel edges land.
2. **Per-node dimensional scores in the report** — should `mdk eval` show a
   breakdown per node (node A latency: 1.2s, node B: 0.8s) in addition to
   the workflow total? Not in Phase 1; add as a `--verbose` table in Phase 2.
3. **Workflow-level `--compare` regression** — `mdk eval --compare` today
   saves `evals/.last-run.json` adjacent to agent.yaml. For workflows the
   analogous file would be adjacent to `workflow.yaml`. Confirm the path
   convention before Phase 2 ships.

## Why this is the right shape

Three reasons:

1. **No new eval primitives.** `EvalCase`, `DimensionScores`, `EvalSummary`,
   all gate flags, and all display helpers are reused unchanged. The only
   new code is the engine class that wires `WorkflowRunner` into the existing
   scoring loop.
2. **Score what the operator ships.** The workflow's contract with the outside
   world is its `final_state` — what the sink node returns. Scoring that is
   equivalent to writing an integration test against the public API. Per-node
   scoring is unit testing; `mdk eval <agent-dir>` already covers that.
3. **Incrementally adoptable.** An operator with an existing workflow can add
   `evals/dataset.jsonl` + an `evals:` stanza to `workflow.yaml`, run
   `mdk eval .`, and get a score. Phase 2 dimensional features opt in via the
   same dataset fields that agent evals already use.
