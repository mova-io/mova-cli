# Task-Oriented pattern — governance

Topology: `SUPERVISOR (planner) → task-a → task-b → collector`
(a bounded fan-out of two task branches, then a collect step).

This realizes ADR 038's **bounded SUPERVISOR delegation** flagship: a manager
decomposes work to specialists *with* a hard cap on how many — the bounds are
the point (vs AutoGen-style entropy).

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| Fan-out cap / fixed roster | `workflow.yaml` wires exactly TWO task nodes (`task-a`, `task-b`) | The supervisor cannot spawn more branches — the count is structural. Widening it is a reviewable edit to `workflow.yaml`, not a model decision. |
| Total budget cap | each node's `budget.max_cost_usd_per_run: 0.10` | Four nodes × $0.10 ⇒ a $0.40 effective per-run ceiling for the whole workflow. Tighten per node. |
| Typed + traced | every node is a typed `agent` node; the runner opens one `workflow.execute` root span and nests each node under it | Full interaction trace; no node runs untraced. |
| Eval-gate | `evals:` stanza + `evals/judge.yaml.example` (`gate: 0.7`) | The whole workflow is gated at CI time (`mdk eval <workflow> --gate`). |

## Why bounded (not an open swarm)

ADR 038 **declines** open swarms / recursive spawning (Tier-3). This pattern is
the governed alternative: a fixed roster of specialists under one supervisor,
with a structural branch cap and a summed budget ceiling.

## Topology note

A real fan-out **diamond** (ADR 092): `supervisor ⇉ {task-a, task-b} ⇉ collector`.
The supervisor fans out to its fixed roster of two task branches with
`kind: fan_out`; they reconverge on the collector with `kind: fan_in`. The
branches run **in parallel** — durably on Temporal (`asyncio.gather`, ADR 092
Phase 2), concurrently on the native runner (Phase 1). The cap (two task nodes)
is the fan-out roster and the collector is the join — both structural and
reviewable. With `runtime: auto` (ADR 091) this prefers Temporal, where the
parallelism is durable + replayable.

## Run it

```
# zero-cost smoke (one canned response satisfies every node's output schema):
MOVATE_MOCK_RESPONSE='{"plan":"p","task_a_result":"a","task_b_result":"b","answer":"done"}' \
  mdk run <workflow-dir> '{"request": "..."}' --mock
mdk eval <workflow-dir> --mock        # exercises the workflow eval engine
```
