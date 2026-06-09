# ADR 094 — A deterministic `decision` node: value-based routing without an LLM

Status: Accepted
Date: 2026-06-09
Accepted: 2026-06-09 — approved by Jeremy (authoring-surface choice: structured
rules over a string DSL). Closes authoring gap #47 surfaced by the certification
suite (the Expense Approval scenario burned an LLM classifier just to compare a
number).
Deciders: Engineering — additive workflow-node primitive behind the existing
node-type seam (CLAUDE.md §7: "extend via adapters/specs, don't hardcode").
Builds on: ADR 054/055 (Temporal compiler + native runner parity), ADR 056
(`judge.py` as the one shared semantic helper across backends), ADR 091
(Temporal as the default runtime), ADR 092 (DAG routing primitives).

## Context

Branching in an mdk workflow today **requires an LLM**. The only routing
primitive is `intent-router` (`NodeType.INTENT_ROUTER`): it runs a classifier
agent and routes on the predicted label. That is the right tool for genuine
classification ("is this billing or support?"), but it is the *wrong* tool for a
**deterministic value comparison** — "route to the director if `amount > 5000`,
else the manager." Forcing that through an LLM is:

- **Non-deterministic** — the same input can route differently run-to-run, and
  it replays differently on Temporal (a classifier is an activity whose result
  is recorded, but the *decision logic* is a model, not arithmetic).
- **Costly + slow** — a model call (and on Temporal a scheduled activity) for
  what is one numeric comparison.
- **Untestable as policy** — "amounts over 5000 go to a director" is a business
  rule that should be readable in `workflow.yaml` and asserted, not buried in a
  prompt.

The certification suite (`certification/`) made this concrete: the Expense
Approval scenario needed a `tier-classifier` agent purely to compare `amount` to
a threshold — the #1 "runtime is ahead of authoring" gap (#47).

Naming note: "gate" is already taken — governance `GateKind` (ADR 093) and the
Temporal compiler's internal `_emit_gate_node` (which lowers `intent-router`).
The new node is therefore **`decision`**.

## Decision

### D1 — A `decision` node with a closed-operator, structured rule surface

A new node type `decision` (`NodeType.DECISION`) routes on an **ordered list of
cases**; the first whose predicate holds wins, else `default`:

```yaml
nodes:
  - id: classify
    type: decision
    cases:
      - when: {field: amount, op: gt, value: 5000}
        to: director-approval
      - when: {field: amount, op: gt, value: 0}
        to: manager-approval
    default: auto-approve
```

`field` is a dotted path into workflow state (`expense.amount`). `op` is one of
a **closed allowlist**: `gt, gte, lt, lte, eq, ne, in, not_in, contains, truthy,
falsy`. `value` is a YAML literal. It is the deterministic twin of
`intent-router`: same routing-table shape (`cases`/`default` ≈ `routes`/
`fallback`), no classifier.

### D2 — Structured rules, not a string DSL

We deliberately reject a string expression surface (`when: "amount > 5000"`). A
DSL would need a parser/evaluator — either `eval` (a security non-starter) or a
hand-rolled mini-parser (real surface area that must stay deterministic and
sandbox-safe). The structured form is **safe by construction** (no eval, no
parser), **deterministic** (a fixed operator set), pins each operator at parse
time via a pydantic `Literal`, adds **zero dependency** (CLAUDE.md §8), and is
trivially validated and linted. Ergonomic cost (slightly more verbose) is worth
the safety + determinism.

### D3 — One shared helper; Temporal lowers it **inline, with no activity**

Both backends route through the single pure helper
`movate.core.workflow.decision.evaluate_decision(cases, default, state)` — the
same "one shape, one rule, no backend invents its own" pattern as `judge.py`
(ADR 056 D2/D3). Because the logic is pure (no time/random/IO, no model call):

- the **native runner** computes the route inline (`_run_decision`), producing
  **no `RunRecord`** (no agent ran);
- the **Temporal compiler** emits a single inline
  `current = evaluate_decision(<cases>, <default>, state)` and **schedules no
  activity** — unlike `intent-router`, which schedules `call_gate_activity`. The
  helper is imported into the generated workflow through
  `workflow.unsafe.imports_passed_through()`, which is safe precisely because the
  module is dependency-free and side-effect-free; replay is deterministic because
  the inputs (the `cases`/`default` literals + the replayed `state`) are
  identical every replay.

Funnelling both backends through the one helper is what guarantees native and
Temporal can never disagree on a branch — the single biggest risk of the change.

**Semantics** (fail-soft + deterministic, identical on both backends):

- Ordered comparisons (`gt/gte/lt/lte`) attempt numeric coercion of both sides
  (so `"5000" > 0` works); if either side is uncoercible the case is a
  **non-match** (fall through) — never an exception that wedges the run.
- A **missing field** is a non-match for comparison/membership ops, and falsy
  for `truthy`/`falsy`.
- `in` ⇒ `field in value` (value is the collection); `not_in` negates it;
  `contains` ⇒ `value in field` (field is the collection/string). The spec
  validator pins the value shape (`in`/`not_in` need a list; `contains` a
  list/str) so a malformed rule fails in `mdk validate`, not silently at runtime.

### D4 — Observability without a RunRecord

The decision node makes no model call, so it has no generation to trace. The
native runner still opens a `workflow.decision` span (nested under the workflow
root) carrying the matched case index (or `"default"`) and the chosen route, so
the branch is visible in Langfuse / Grafana traces. On Temporal the branch is
recorded in workflow history (the `current =` assignment on replayed state), so
it is visible in the Temporal Web UI.

### D5 — Compatibility: purely additive

A new `NodeType` member, a new `DecisionNodeSpec` in the discriminated union
(every existing spec keeps `extra="forbid"`), and new arms in the compiler /
native runner / Temporal compiler / `validate`. No change to `agent.yaml` /
`project.yaml`, the `/api/v1` runtime API, storage schema, CLI flags, or env
vars. Every existing workflow compiles **byte-for-byte unchanged** (the Temporal
header import and emit are gated on the presence of a decision node). CalVer is
git-derived; no version line.

## Boundary (out of scope)

A decision node routes on **state values**, not on a human's free-text response.
Routing on a HUMAN node's `decision` field (approve/reject) is genuine
classification and stays an `intent-router` — that is gap #48. Convergent
branches collapsing onto a shared sink is gap #49 (joins/fan-in). The decision
node closes **#47 only**.

## Consequences

- The certification Expense Approval scenario drops its `tier-classifier` LLM hop
  (one fewer node, one fewer model call on the hot path) and becomes
  deterministic on the tiering decision.
- Authors get a readable, testable business-rule primitive; "amounts over 5000
  need a director" lives in `workflow.yaml` and is covered by a unit test.
- The pattern generalizes: any pure value gate (status routing, size
  thresholds, flag checks) no longer needs an LLM.
