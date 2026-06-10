# ADR 099 — HUMAN-node decision routing: route on the approver's structured answer without an LLM

Status: Accepted
Date: 2026-06-09
Accepted: 2026-06-09 — approved by Jeremy. Ratified: unmatched decision → accept + fallback (not 422); casefold+trim matching; route_on defaults to 'decision'.
Deciders: Engineering — additive fields on an existing node spec behind the
existing node-type seam (CLAUDE.md §7: "extend via adapters/specs, don't
hardcode").
Builds on: ADR 017 D5 (HUMAN gate pause/resume), ADR 062 (durable HITL —
`wait_condition` + typed signal, `timeout`/`on_timeout`), ADR 055 D7
(native/Temporal traversal parity), ADR 094 (deterministic `decision` node +
`decision.py` as the shared routing helper). Closes authoring gap #48, which
ADR 094 explicitly deferred ("Routing on a HUMAN node's `decision` field …
is gap #48").

## Context

A HUMAN (HITL) node cannot route on its own decision. After the approver
signals, **both** backends advance to the gate's single sequential successor:

- native — `WorkflowRunner.resume` continues from
  `_sequential_successor(graph, record.paused_node_id)`
  (`src/movate/core/workflow/runner.py:255`);
- Temporal — `_emit_human_node` emits `current = {successor!r}` after the
  `wait_condition` resolves (`src/movate/core/workflow/compilers/temporal.py:1064-1070`),
  with an explicit deferral in its docstring: "Gate-style `routes` are
  intentionally NOT emitted here … Routed HUMAN nodes would need a matching
  native change and are deferred."

So every approve/reject gate today needs a **second node — an LLM** — just to
read the answer it asked for. The canonical shape is

```
manager-approval (human) → manager-decision (intent-router + classifier LLM) → post-erp | rejected
```

— a model call to classify `"approve"` as approve. That is absurd for a
*structured* decision: the gate's `prompt` already tells the approver to answer
with `decision: "approve" | "reject"`, the signal endpoint already validates the
key is present against `output_contract`
(`src/movate/runtime/app.py:13963-13975`), and ADR 094 just established that
deterministic value routing needs no LLM. The certification Expense Approval
scenario and the ITSM family all carry this dead-weight classifier hop.

Authors who try the obvious thing hit a wall: adding `routes:`/`fallback:` to a
`human` node fails `mdk validate` with pydantic's "Extra inputs are not
permitted" — `HumanNodeSpec` is `extra="forbid"`
(`src/movate/core/workflow/spec.py:142`), and carries only `prompt`,
`output_contract`, `approvers`, `timeout`, `on_timeout`
(`spec.py:125-198`).

What already exists, verified:

- **The routing mechanism is already half-emitted.** `_emit_human_node`'s
  timeout branch already routes: on expiry it emits
  `current = {on_timeout!r}` (`compilers/temporal.py:1075-1085`). Only the
  delivered-decision arm hard-codes the sequential successor. There is **no**
  partial `routes` support in the emitted code — the deferral note is accurate.
- **The decision payload is already in state at routing time.** The signal
  endpoint merges the decision into `paused_state` before resume
  (`app.py:13981-13990`); the Temporal signal handler stores it and the merge
  lines `state.update(...)` it before `current` is assigned
  (`temporal.py:1064-1067`). Both backends therefore have the decision value in
  hand exactly where the route would be computed.
- **The shared-helper precedent exists.** ADR 094's
  `movate.core.workflow.decision` module is the dependency-free, pure routing
  helper both backends funnel through (`decision.py:152-160`); the Temporal
  compiler already imports it into the workflow sandbox via
  `workflow.unsafe.imports_passed_through()` gated on node presence
  (`temporal.py:429-435`).
- **The routing-table vocabulary exists.** `IntentRouterNodeSpec` already
  spells it `routes: dict[str, str]` + `fallback: str` (`spec.py:101-102`);
  authors know the shape.

## Decision

### D1 — Additive `routes` / `fallback` / `route_on` on `HumanNodeSpec`, exact-match semantics

Three optional fields on the HUMAN node (same vocabulary as `intent-router`):

```yaml
nodes:
  - id: manager-approval
    type: human
    prompt: |
      Approve this expense? Respond with `decision` ("approve" or "reject").
    output_contract: [decision, approver]
    approvers: [finance-approver]
    route_on: decision            # optional; defaults to "decision"
    routes:
      approve: post-erp
      reject: rejected
    fallback: rejected            # required when routes is set

  - id: post-erp
    type: agent
    ref: ./agents/post-erp
  - id: rejected
    type: agent
    ref: ./agents/rejected
```

Matching is **deterministic exact match, normalized for human input**: the
routed value is `str(state[route_on]).strip().casefold()` compared against the
route keys casefolded. Rationale: the value is typed or button-pressed by a
person — `"Approve "` and `"approve"` must not route differently — but anything
beyond trim + case-fold (synonyms, sentiment, prose) is *classification* and
stays an `intent-router`'s job. That is the boundary statement: **`routes` on a
HUMAN node handles a closed answer vocabulary; free-text interpretation keeps
the LLM classifier.** No-match (including a missing/empty `route_on` key) takes
`fallback` — never an exception that wedges the run, mirroring ADR 094 D3's
fail-soft rule.

Spec-level validation (compile-time, `extra="forbid"` preserved):

- `fallback` is required when `routes` is set (and `routes`/`route_on` are
  rejected without each other where meaningless) — same pattern as the existing
  `timeout`/`on_timeout` pair validator (`spec.py:189-198`).
- Route keys must be unique **after** casefold (so `Approve:`/`approve:` can't
  silently shadow each other).
- `route_on` must be listed in `output_contract`. This is load-bearing for D3:
  the signal endpoint's existing 422 ("decision is missing required
  output_contract key(s)") then guarantees a delivered decision always carries
  the routing key, with zero new endpoint logic.
- Route targets + `fallback` must name valid node ids, validated in
  `compile_workflow` exactly like decision-node targets
  (`compiler.py:285-295`), and synthetic `CONDITIONAL` edges
  (`{"synthetic": True, "source": "human"}`) are injected for each target so
  reachability/topological order stay correct — byte-for-byte the decision-node
  pattern (`compiler.py:389-408`).

### D2 — One shared helper drives both backends (precedent: `decision.py`)

A new pure function in `movate.core.workflow.decision`:

```python
def evaluate_human_route(routes: dict[str, str], fallback: str, value: Any) -> str:
    """Exact-match route for a HUMAN gate's decision value: trim + casefold,
    first the matching route key, else fallback. Pure + total — never raises."""
```

It lives next to `evaluate_decision` (`decision.py:152-160`) because that module
is the established "one shape, one rule, no backend invents its own" seam
(ADR 094 D3, mirroring `judge.py` per ADR 056): dependency-free,
side-effect-free, already passed through Temporal's deterministic-workflow
sandbox.

- **Native** — `WorkflowRunner.resume` (`runner.py:206-263`): after building
  `resume_state`, if the paused node's metadata carries `routes`, the
  continuation start becomes
  `evaluate_human_route(routes, fallback, resume_state.get(route_on))` instead
  of `_sequential_successor(...)` (`runner.py:255`). Everything else —
  checkpoint guards, `_walk`, re-pause on a successor gate — is untouched.
- **Temporal** — `_emit_human_node` (`temporal.py:1003-1093`): when `routes`
  is present, the delivered-signal merge lines emit
  `current = evaluate_human_route({routes!r}, {fallback!r}, state.get({route_on!r}))`
  instead of `current = {successor!r}`. The helper import is added to the
  passed-through block gated on a routed HUMAN node being present, exactly as
  `evaluate_decision` is gated today (`temporal.py:429-435`). Replay is
  deterministic: the inputs are emitted literals + replayed state.
- **Compiler** — `compile_workflow` stamps `routes`/`fallback`/`route_on` into
  the HUMAN node's metadata **only when set** (the existing only-stamp-when-set
  rule for ADR 062 extras, `compiler.py:135-144`), so an unrouted gate's
  metadata stays byte-identical.

Funnelling both backends through the one helper is what guarantees they can
never disagree on a branch — the same parity argument as ADR 094 D3 / ADR 055
D7, and the exact native+Temporal pairing the `_emit_human_node` deferral note
said routed HUMAN nodes would need.

### D3 — Signal endpoint: accept + fallback, do not 422 on an unmatched value

`POST /api/v1/workflow-runs/{id}/signal` keeps its existing checks (404 / 409 /
422-on-missing-`output_contract`-key, `app.py:13893-13975`) and gains **no
routing-value validation**. An approver's decision that matches no route key is
**accepted (202) and routes to `fallback`**.

Why not 422:

- The human may type prose (`"approved, looks fine — Dana"`). Rejecting the
  signal wedges a gate that ADR 062 deliberately made resilient; `fallback` is
  the author's *declared* answer to "what if the response isn't in the
  vocabulary," and the author who wants strictness points `fallback` at a
  re-prompt/escalation node.
- It keeps the endpoint backend-agnostic: the route is computed where each
  backend resumes (worker / Temporal workflow), not in the control plane — the
  endpoint stays a transport (ADR 062 D2), and native and Temporal cannot drift
  on what "valid" means because the endpoint never decides it.
- The missing-key case is already covered: D1 requires `route_on` ∈
  `output_contract`, so the existing 422 fires when the routing key is absent
  entirely.

The resolved route is observable, not silent: the native resume opens the
workflow span as today, and a `workflow.human_route` span attribute (matched
key or `"fallback"`) mirrors ADR 094 D4's `workflow.decision` observability; on
Temporal the branch is the `current =` assignment in history.

### D4 — Interaction with `timeout` / `on_timeout`: timeout wins, routes need a delivered decision

`routes` applies **only to a delivered decision**. If the durable `timeout`
(ADR 062 D4) expires first, the node takes `on_timeout` exactly as today — the
emitted `except asyncio.TimeoutError: current = {on_timeout!r}` arm
(`temporal.py:1075-1085`) is untouched; routed emission changes only the
`else:` (signal-delivered) arm. The two route tables never compose: `on_timeout`
answers "nobody decided," `routes` answers "what was decided." Native has no
durable timer (ADR 062 D4 is Temporal-only), so native semantics gain routing
with no timeout interaction at all.

(Adjacent debt, documented not fixed per CLAUDE.md §4: `on_timeout` targets get
no compile-time node-id validation and no synthetic edge today —
`compiler.py:144` stamps the value unchecked. D1's target validation covers
`routes`/`fallback` only; extending it to `on_timeout` is a separate small fix.)

### D5 — Compatibility: purely additive

Three optional fields on `HumanNodeSpec` (`extra="forbid"` kept); no change to
`agent.yaml`/`project.yaml`, the `/api/v1` API (the signal request/response
shapes are untouched), storage schema (`routes` ride the node metadata already
persisted in the graph, not `WorkflowRunRecord`), CLI flags, or env vars. Every
existing HUMAN node — none declares the new fields, enforced by today's
`extra="forbid"` — compiles to **byte-identical** metadata and byte-identical
emitted Temporal code (the new emission and the helper import are gated on
`routes` being present). CalVer is git-derived; no version line.

## Boundary (out of scope)

- **Free-text interpretation.** Mapping `"ship it"` → approve is
  classification; that remains `intent-router` + classifier agent. `routes` is
  for a closed, prompt-declared answer vocabulary only.
- **Comparison operators on the human response** (`amount_granted > 5000`).
  That is a `decision` node — and it already works today by placing one *after*
  the gate, since the decision is merged into state (see Alternatives).
- **Multi-key routing** (route on `decision` *and* `severity`). One key per
  gate; compose with a downstream `decision` node if genuinely needed.
- **Native durable timeouts** — unchanged from ADR 062 D4.

## Alternatives considered

- **Status quo — keep the LLM classifier hop.** Every structured gate pays a
  model call, a node, and non-determinism to parse an answer the gate itself
  constrained. Rejected: ADR 094 already established the principle that
  deterministic routing must not cost an LLM; this is the same gap one node
  earlier.
- **HUMAN gate + downstream `decision` node (today's no-LLM workaround).**
  `manager-approval (human) → route (decision, eq on state.decision) → …`
  works **today** with zero changes, and is the right answer when routing needs
  operators or multiple fields. Rejected as the *blessed* answer for the
  approve/reject case: it still costs an extra node per gate, `eq` is raw
  (case-sensitive, no trim — hostile to typed input), and the gate's outcomes
  belong on the gate — `timeout`/`on_timeout` already set the precedent that a
  HUMAN node declares where its own resolutions go.
- **Making HUMAN a special case of `decision`** (a `decision` node that pauses
  first, or full `cases:` syntax on the human node). Rejected: it entangles two
  orthogonal primitives — the pause/signal lifecycle (checkpoint, approvers,
  notifier, durable timer) and pure value routing — across the spec, runner,
  compiler, and signal endpoint, and the closed-allowlist `cases` surface is
  overkill for "approve|reject". The flat `routes`/`fallback` map matches
  `intent-router` (`spec.py:101-102`), so authors swap a classifier gate for a
  human gate by changing the node type and deleting the LLM.

## Consequences

- Every structured HITL flow drops **one LLM node and one model call**: the
  expense-approval shape collapses
  `manager-approval (human) → manager-decision (intent-router) → post-erp | rejected`
  into a single routed HUMAN node (2 nodes per gate across the ITSM family the
  same way), and the approve/reject branch becomes deterministic and replayable.
- The approval policy is readable in `workflow.yaml` and unit-testable
  (`evaluate_human_route` is pure), instead of buried in a classifier prompt.
- One more consumer of `decision.py` cements it as the shared routing-semantics
  module across backends (ADR 056 → 094 → this).
- Implementation surface (one PR): `spec.py` (fields + validators),
  `compiler.py` (metadata stamp, target validation, synthetic edges),
  `decision.py` (`evaluate_human_route`), `runner.py` (`resume` successor
  selection), `compilers/temporal.py` (`_emit_human_node` else-arm + gated
  import), plus spec/helper/native-resume/emitted-code/conformance tests. No
  storage, API, or CLI changes.
