# Chatbot pattern — governance

Topology: `INPUT → AGENT → OUTPUT` (single agent, one turn).

This is the governable baseline from ADR 038: the simplest pattern that is still
*observable, evaluable, bounded, and deployable*.

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| Per-run cost cap | `agent.yaml: budget.max_cost_usd_per_run: 0.10` | A single turn can never overspend; the executor halts on breach. |
| Output token bound | `agent.yaml: model.params.max_tokens: 512` | Caps response size (cost + latency). |
| Enforced output contract | `schema/output.yaml` | The executor validates every reply; a non-conforming response fails the run rather than reaching the user. |
| Eval-gate | `objectives[].threshold: 0.7` + `evals/judge.yaml.example` | The reply quality is gated at CI time (`mdk eval --gate`). |

## Why no loop / tools / fan-out

The Chatbot pattern is deliberately single-shot. Anything that adds a loop, a
tool call, a fan-out, or an iterating supervisor moves you to one of the other
four pattern templates (task-oriented, goal-oriented, monitor, simulation),
each of which carries its own additional bounds. Keeping this pattern minimal
keeps its blast radius minimal.

## Run it

```
mdk run <name> '{"message": "hello"}' --mock      # zero-cost smoke
mdk eval <name> --mock                             # gate the dataset offline
```
