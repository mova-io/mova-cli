# Simulation pattern — governance

Topology:
`SUPERVISOR → turn-1-a → turn-1-b → turn-gate-1 → turn-2-a → turn-2-b → turn-gate-2 → {done-resolved-early | done-resolved-late | done-capped}`
(a fixed roster of two participants, two hard-capped turns, a terminating JUDGE).

This is a **BOUNDED** multi-agent simulation — the governance is the headline.

## Why bounded, and why an open swarm is not

ADR 038 D5 / Tier-3 **declines** open swarms, autonomous collaboration, debate,
and recursive spawning: they fail the governable filter (observable + evaluable
+ bounded + deployable). This pattern is the governed alternative — every degree
of freedom a swarm would have is removed and replaced with a structural bound:

| A swarm would… | This pattern instead… |
|---|---|
| spawn agents dynamically | uses a **FIXED ROSTER** of two participants wired in `workflow.yaml`; no node can spawn another. |
| run until "done" (unbounded) | has a **HARD TURN CAP = 2** (the loop is unrolled; after the last turn every route leads to a terminal — no path back to a participant). |
| self-terminate by consensus | terminates only via the **JUDGE/GATE** (`resolved`) or the turn cap. |
| have no cost ceiling | carries a **HARD BUDGET CAP** per node (`budget.max_cost_usd_per_run`), summing to the simulation's ceiling. |
| be hard to trace | produces a **FULL INTERACTION TRACE**: one workflow-root span with every participant/judge turn nested under it. |

Cycles are impossible by construction — the native runner (ADR 017) rejects
cyclic graphs at compile time, so a runaway simulation cannot exist.

## Bounds baked in

| Bound | Where |
|---|---|
| Fixed roster (2 participants) | `workflow.yaml` wires exactly `participant-a` + `participant-b`. |
| Hard turn cap = 2 | two unrolled `a → b → gate` turns; no edge returns to a participant after turn 2. |
| Terminating JUDGE | each `turn-gate-*` is an `intent-router` (the JUDGE); it is the only turn exit. |
| Hard budget cap | per-node `budget.max_cost_usd_per_run` (judge 0.05, others 0.10). |
| Eval-gate | `evals:` stanza + `evals/judge.yaml.example` (`gate: 0.7`). |

## Mock note

`mdk run --mock` exercises the JUDGE/GATE classifiers through the MockProvider,
so the mock response must ALSO carry a `label` the gate can route on. With
`label: "continue"` the smoke walks BOTH turns and exits at `done-capped`,
exercising the full bounded simulation and the turn cap. (The unified mock
response carries every node's required key — `additionalProperties: true` on
each schema lets one response satisfy them all.)

## Run it

```
MOVATE_MOCK_RESPONSE='{"transcript":"A:..\nB:..","outcome":"resolved","label":"continue"}' \
  mdk run <workflow-dir> '{"scenario": "..."}' --mock
mdk eval <workflow-dir> --mock
```
