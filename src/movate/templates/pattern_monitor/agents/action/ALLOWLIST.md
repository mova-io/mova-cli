# Action allowlist — Monitor pattern

The action node may take ONLY the actions on this list. This is the governance
boundary for the pattern's one side-effecting step.

| Action | Effect | Side-effect class |
|---|---|---|
| `notify-oncall` | Page the on-call engineer | network (write) |
| `open-incident` | Open a tracked incident | network (write) |
| `scale-out` | Request additional capacity | mutates-state |
| `throttle-ingress` | Shed load at the edge | mutates-state |

## How the allowlist is enforced

1. **Today (stub):** `agents/action/prompt.md` constrains the model to choose
   only from this list, and the node emits the action it *would* take as an
   audit record — it performs no real side effect.
2. **When wired to a real skill:** declare each action as a `skills/<name>/`
   entry with an honest `side_effects:` class, then gate the project with
   `project.yaml: skills.allowed_side_effects:` so only vetted classes can run.
   That makes the allowlist a *hard* gate enforced by `mdk validate` and the
   executor, not just a prompt convention.

Turning the stub into a live action is a deliberate, reviewable change — never
let the model expand this list at runtime.
