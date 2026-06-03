# Chatbot pattern

Topology: `INPUT -> AGENT -> OUTPUT` (single agent, one turn).

This template scaffolds a single agent (`agent.yaml`), not a workflow. The
runtime is therefore implicit: there is no `runtime:` field on `agent.yaml`.
A chat turn is one pass through the agent's executor — see ADR 038 §F1 for
the governed shape and `GOVERNANCE.md` for the bounds.

## Run it

```
mdk run <name> '{"message": "hello"}' --mock      # zero-cost smoke
mdk eval <name> --mock                             # gate the dataset offline
```

## If this chatbot grows into a workflow

The moment the topology adds a second node (a tool step, a JUDGE/GATE, a
SUPERVISOR, a fan-out), the template stops being "a single agent" and becomes
"a workflow". At that point, add a `workflow.yaml` next to `agent.yaml` and
pick an execution runtime at the top of it:

```yaml
# workflow.yaml
api_version: movate/v1
kind: Workflow

name: my-chatbot
version: 0.1.0

# Execution runtime — picked per workflow.
# native    = in-process (default, fast, ephemeral)
# langgraph = export-and-execute via LangGraph (ADR 030)
# temporal  = deterministic, durable, replayable (ADR 054)
# Flip to `runtime: temporal` for durable multi-day execution + replay debugging.
# runtime: native
```

The other four pattern templates (`pattern_task_oriented`,
`pattern_goal_oriented`, `pattern_monitor`, `pattern_simulation`) already
carry this comment at the top of their `workflow.yaml` — match that shape.

A chatbot specifically rarely needs `temporal`; it is included for
completeness and for the case where a chat surface fronts a durable
back-end workflow. See ADR 054 §"Patterns × runners: the cross product"
for the matrix.
