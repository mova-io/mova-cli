# Goal-Oriented pattern — governance

Topology: `SUPERVISOR → worker-1 → goal-gate-1 → worker-2 → goal-gate-2 → {done-satisfied-early | done-satisfied-late | done-maxed}`
(a bounded, unrolled iteration loop with a JUDGE/GATE after each step).

This realizes ADR 038's **inline JUDGE/GATE** flagship combined with bounded
iteration: a judge runs *inside the live pipeline* and is the only loop exit.

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| max_iterations = 2 | `workflow.yaml` unrolls exactly two `worker → goal-gate` iterations | The loop length is structural. After iteration 2 every route leads to a terminal — there is no path back to a worker, so no unbounded loop. Widening the cap is a reviewable edit. |
| GATE is the only exit | each `goal-gate-*` is an `intent-router` (the JUDGE/GATE) | An iteration can only be left via the gate's verdict (`satisfied` → done, `continue` → next iteration). |
| No cycles | the native runner (ADR 017) rejects cyclic graphs at compile time | A runaway loop is impossible by construction — the firewall is the engine itself. |
| Budget cap | each node's `budget.max_cost_usd_per_run` (judge: 0.05, others 0.10) | Six nodes sum to a bounded per-run ceiling (see the run output's cost). |
| Eval-gate | `evals:` stanza + `evals/judge.yaml.example` (`gate: 0.7`) | The whole workflow is gated at CI time. |

## Distinct exits = distinct terminals

`done-satisfied-early` / `done-satisfied-late` (the JUDGE said the goal was met
on iteration 1 / 2) and `done-maxed` (the max-iterations cap tripped) are
separate terminal nodes so the exit *reason* is observable in the trace, and so
the graph stays join-free (each terminal has exactly one predecessor — its
gate). All three share the same `agents/done` implementation.

## Mock note

`mdk run --mock` exercises the JUDGE/GATE classifiers through the MockProvider,
so the mock response must ALSO carry a `label` the gate can route on. With
`label: "continue"` the smoke walks BOTH iterations and exits at `done-maxed`,
exercising the full unrolled loop and the max-iterations cap. (The unified mock
response carries every node's required key — `additionalProperties: true` on
each schema lets one response satisfy them all.)

## Run it

```
MOVATE_MOCK_RESPONSE='{"attempt":"draft","result":"final","label":"continue"}' \
  mdk run <workflow-dir> '{"goal": "..."}' --mock
mdk eval <workflow-dir> --mock
```
