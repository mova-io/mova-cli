# Monitor pattern — governance

Topology: `observer → threshold-gate → {action | no-op}`
(observe a signal, VALIDATE/GATE it against a threshold, act only on breach).

This realizes ADR 038's inline `VALIDATE`/`GATE` as runtime governance: the gate
sits *inside the live pipeline*, and the one side-effecting step (`action`) is
reachable only through it.

## Bounds baked in

| Bound | Where | Why |
|---|---|---|
| Action allowlist | `agents/action/prompt.md` + `agents/action/ALLOWLIST.md` | The action node may take ONLY listed actions. It is a STUB (emits the action it would take); wiring it live is gated by `project.yaml: skills.allowed_side_effects`. |
| Gate-only action | `threshold-gate` (intent-router) is the only edge into `action` | No action fires without a breach verdict. The graph is join-free, so `action` has exactly one predecessor (the gate). |
| Audit note | `action` emits `action_taken` (action + justification); the runner traces every node under one workflow-root span | Each breach→action is auditable. |
| Budget cap | per-node `budget.max_cost_usd_per_run` (observer/gate/action 0.05, no-op 0.02) | Bounded per-run cost. |
| Eval-gate | `evals:` stanza + `evals/judge.yaml.example` (`gate: 0.7`) | The whole workflow is gated at CI time, including a check that the chosen action is *allowlisted*. |

## Schedule / trigger friendly

The pattern runs on the generic scheduler/trigger primitives (ADR 017 D2) —
there is no bespoke monitor daemon. See `schedule.yaml.example` for the
`mdk schedule set` / `mdk trigger create` wiring.

## Mock note

`mdk run --mock` exercises the VALIDATE/GATE classifier through the
MockProvider, so the mock response must ALSO carry a `label` the gate can route
on. With `label: "breach"` the smoke exercises the breach→action path — the one
the governance is about. (The unified mock response carries every node's
required key — `additionalProperties: true` on each schema lets one response
satisfy them all.)

## Run it

```
MOVATE_MOCK_RESPONSE='{"metric":"error_rate=0.12","action_taken":"open-incident: breach","label":"breach"}' \
  mdk run <workflow-dir> '{"signal": "..."}' --mock
mdk eval <workflow-dir> --mock
```
