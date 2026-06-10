# ADR 097 — A first-class `tool` node: deterministic skill execution as a workflow step

Status: Accepted
Date: 2026-06-09
Accepted: 2026-06-09 — approved by Jeremy. All recommendations ratified (raw-merge default + opt-in output_key, activity-side mapping via shared helpers, compile-time resolution, no RunRecord).
Deciders: Engineering — additive workflow-node primitive behind the existing
node-type seam (CLAUDE.md §7: "extend via adapters/specs, don't hardcode").
Closes authoring gap #50 surfaced by the certification suite (an external call —
an order lookup, a price check, a CRM write — must today be wrapped in an LLM
agent whose only job is to invoke one skill).
Builds on: ADR 002 (skills + the `SkillBackend` Protocol / `dispatch_skill`),
ADR 054/055 (Temporal compiler + native runner parity; `call_skill_activity` is
already wired — D4 row 7), ADR 091 (Temporal as the default runtime), ADR 093
(governance gates; the SKILL gate), ADR 094 (the `decision` node — the playbook
this ADR follows).

## Context

Calling a deterministic skill from a workflow today **requires an LLM**. The
only executable node type is `agent` (`NodeType.AGENT`): to fetch an order from
an API mid-workflow, an author must scaffold an agent whose prompt says "call
the `order-lookup` tool with the order id and return its output verbatim" and
hope the model complies. That is the wrong tool for a **deterministic external
call**:

- **Non-deterministic** — the model may rephrase, truncate, or hallucinate
  around the tool output; the same input can produce different state run-to-run.
- **Costly + slow** — a full LLM turn (and its tokens) to do what is one
  function dispatch.
- **A governance blind spot in waiting** — the "wrapper agent" pattern hides an
  external side-effect behind a prompt instead of declaring it in
  `workflow.yaml` where `mdk validate` and the SKILL gate can see it.

The machinery for the fix **already half-exists**, deliberately staged by
earlier ADRs:

- `NodeType.TOOL = "tool"` is declared in the IR (`ir.py`) — reserved, rejected
  by `validate_linear` today.
- The Temporal compiler is already total over it: `_emit_node` routes
  `NodeType.TOOL` to `_emit_skill_node`, which lowers to
  `await workflow.execute_activity(call_skill_activity, args=[node_id, ref,
  state, run_id], ...)`.
- `call_skill_activity` (`temporal_activities.py`) is live: it `load_skill`s
  the directory in `node.ref`, narrows `state` to the skill's input-schema
  properties (the same projection rule agents get), and dispatches through the
  one shared `movate.core.skill_backend.base.dispatch_skill` — the identical
  path the Executor's tool-use loop uses. Its own docstring says it "lights up
  for free when TOOL nodes ship".

What is missing is the **authoring surface** (a `ToolNodeSpec`), the
**compile-time resolution** (skill *name* → skill *directory*), the **native
runner arm** (parity with the Temporal path), and the **governance hookup**
(today `call_skill_activity` bypasses the SKILL gate entirely, because the gate
lives in `Executor.execute()` which a standalone skill call never enters).

Skills themselves are unchanged: `skills/<name>/skill.yaml` with
`kind: python | http | mcp | agent | workflow | exec | langchain`, JSON-Schema
input/output contracts, a declared `side_effects` class, and an optional
`capabilities: {deterministic: …}` block. The tool node is a *new caller* of an
existing primitive, not a new primitive.

## Decision

### D1 — A `tool` node that names a registered skill; projection in, merge out

A new authoring node type `tool` (adopting the reserved `NodeType.TOOL`)
executes one skill as a workflow step — no LLM, no prompt:

```yaml
nodes:
  - id: fetch-order
    type: tool
    skill: order-lookup          # registry name — same name agents put in skills: [...]
    # input: / output_key: are OPTIONAL — see below
```

**Input (default):** the skill's input is the current workflow state **narrowed
to the skill's declared input schema** (`properties` keys) — byte-for-byte the
projection rule `call_skill_activity` already implements and the same rule
agent nodes get (`_project_state`). A skill with no `properties` receives the
whole state. Authors whose state keys don't line up with the skill's schema add
an explicit map; values are dotted state paths, with a `{literal: …}` wrapper
for constants:

```yaml
  - id: fetch-order
    type: tool
    skill: order-lookup
    input:
      order_id: order.id              # dotted path into workflow state
      include_history: {literal: true}
```

When `input:` is present it is **exclusive** — only the mapped keys are sent
(no implicit projection underneath), so the skill's input is fully readable
from `workflow.yaml`. A mapped path that is missing from state at runtime is
omitted from the input (the skill's own `required` schema then fails the call
loudly via `dispatch_skill`'s input validation — the contract error names the
skill and the key).

**Output (default):** the skill's (schema-validated) output dict merges into
state with `state.update(output)` — the existing `call_skill_activity` /
agent-node contract. For collision safety, `output_key` namespaces the whole
output dict under one state key instead:

```yaml
    output_key: order                  # state["order"] = <skill output dict>
```

We keep raw-merge as the default deliberately: workflow state is a shared
keyspace by design (agents already raw-merge), and the skill's output schema
makes the contributed keys statically knowable — `mdk validate`'s
state-threading lint (D6) sees them either way. `output_key` is the explicit
opt-in for skills with generic output keys (`result`, `status`) that would
clobber.

### D2 — Resolution by registry name, at compile time, workflow-local first

`skill:` is a **name**, not a path — the same vocabulary agents use in
`skills: [...]`. `compile_workflow` resolves it to an absolute skill directory
and bakes that into `node.ref` (exactly the string `call_skill_activity`
expects), failing loud on a miss like an agent-ref typo does:

1. `<workflow_dir>/skills/<name>/` — a **workflow-local** skills dir,
   paralleling the existing workflow-local `agents/` convention
   (`workflows/refund-approval/agents/triage`). Wins on collision.
2. `load_skill_registry(_resolve_project_root(workflow_dir))` — the project
   `skills/` root, found by the same `project.yaml`/`policy.yaml` marker
   walk-up agent skill resolution uses (`core/loader.py`).

Compile-time (not run-time) resolution is correct on both backends because
compile and execution share a filesystem in every deployment shape we have:
the native runner compiles immediately before walking, and the Temporal worker
compiles every `runtime: temporal` workflow **at its own startup** from
`MOVATE_WORKFLOWS_PATH` (`/app/workflows`, ADR 088), then runs it in the same
container. The workflow-local tier is what makes the deployed story work with
zero new bundling: the Dockerfile's existing `COPY workflows/ /app/workflows/`
ships `workflows/<name>/skills/` for free, whereas the project-level `skills/`
root has no marker file under `/app` (uploaded skills land at
`<agents_path>/skills/` for *agent* resolution — a workflow walk-up will not
find them). Deployed Temporal workflows should therefore carry their skills
workflow-locally; `mdk validate` warns when a `tool` node in a
`runtime: temporal` workflow resolves to a project-level skill that the image
bake would not include.

The compiler also stamps `node.metadata` with everything downstream consumers
need without re-loading the skill at validate time: `skill` (the name),
`side_effects` (for the static SKILL gate, D5), `capabilities` (for the
determinism lint, D6), `timeout_call_ms`, and the `input` map / `output_key`
(for the runner and the activity, D3). This is precisely the "metadata field"
the Temporal compiler's TOOL comment reserved.

### D3 — One shared execution path: `dispatch_skill`, with shared pure mapping helpers

There is exactly one way a skill executes in mdk —
`skill_backend.base.dispatch_skill` (input-validate → backend → output-validate,
the five-value `SkillError` taxonomy) — and the tool node does not add a second
one (ADR 002; CLAUDE.md §6/§7). What both backends add around it is identical
and funnels through a new dependency-free module
`movate.core.workflow.tool` (the `decision.py` pattern, ADR 094 D3):

- `build_skill_input(state, input_map, input_schema_props) -> dict` — the
  explicit map (dotted-path / literal) when present, else the schema
  projection.
- `merge_tool_output(output, output_key) -> dict` — the state **delta**:
  `{output_key: output}` when set, else `output` unchanged.

**Native runner** — a `_run_tool` arm in `_walk_inner` (the dispatch ladder at
`runner.py` ~L375–575): `load_skill(node.ref)`, build the input, construct a
`SkillExecutionContext` (run_id = the workflow run id, tenant, storage, tracer,
`mock` flag, and `call_ms_budget` from the skill's `timeout_call_ms` — default
30 s otherwise), `await dispatch_skill(...)`, merge the delta, advance to the
sequential successor.

**Temporal** — `_emit_skill_node` stays an `execute_activity(call_skill_activity,
…)` call; the activity signature grows two **additive, defaulted** args
(`input_map: dict | None = None`, `output_key: str | None = None` — appended,
never reordered, per the lockstep rule in `temporal_activities.py`), and the
activity body calls the same two helpers around its existing
`load_skill`/`dispatch_skill` core. The workflow still does
`state.update(<result>)` — the activity now returns the *delta*, so the
generated workflow shape is unchanged. The old 4-arg call (hand-built graphs,
already-compiled workflows) behaves byte-for-byte as before: no map ⇒ schema
projection, no `output_key` ⇒ raw merge.

Same helpers, same `dispatch_skill`, same projection rule ⇒ native and Temporal
cannot disagree on what the skill saw or what state became — the parity
guarantee this codebase enforces for every node type.

This work also fixes a latent inconsistency in the existing activity:
`call_skill_activity` ignores the skill's declared `timeout_call_ms` today
(hard-coded 30 s context default); both paths now honor it.

### D4 — Failure semantics: a failing skill is a failing node

A `SkillError` from `dispatch_skill` (validation failure, backend error,
timeout) fails the workflow **at that node** — the agent-node contract:

- **Native:** `_run_tool` does not catch-and-continue. The runner persists a
  `WorkflowRunRecord` with `status=ERROR`, `error_node_id=<node id>`, and an
  `ErrorInfo` carrying the `SkillErrorType` + message; partial state (everything
  merged *before* this node) is retained, exactly like an agent-node failure at
  `runner.py` ~L578–607. One attempt — `dispatch_skill` has no retry natively,
  matching the Executor's tool-use loop.
- **Temporal:** the activity raises (the `SkillError` re-raised as a
  `RuntimeError` naming node, skill, and error type — mirroring
  `call_agent_activity`'s failure surfacing), so the compiler-emitted
  `_RETRY_POLICY` (3 attempts, ADR 054 D9 Phase 1 global defaults) retries it;
  exhausted retries fail the workflow with the node attributable in history.

Per-node retry/timeout overrides are **not** invented here: ADR 054 D9 shipped
fixed Phase 1 defaults and explicitly reserved per-node overrides as the
Phase 3 surface (`compilers/temporal.py` ~L403, ~L1198) — the tool node adopts
whatever that surface ships, like every other activity-backed node. The
native/Temporal retry asymmetry (1 attempt vs 3) is the same asymmetry agent
nodes have today and is governed by the same future knob; a skill that is not
idempotent should declare `capabilities.deterministic: false` and gets linted
(D6).

### D5 — Governance: the SKILL gate fires for a tool node, statically and at runtime

A skill invoked by a workflow node must clear **the same** policy a skill
invoked by an agent clears (ADR 093 D6) — otherwise the tool node becomes the
way around `skill_policy`. Two layers, matching the existing belt-and-braces
shape:

- **Static (`mdk validate` / compile):** the workflow validate path checks
  `SkillPolicy.check_skill(name, side_effects)` for every `tool` node, using
  the `side_effects` the compiler stamped into `node.metadata` — the analogue
  of the agent-bundle check `mdk validate` already runs.
- **Runtime:** the execution path checks `skill_policy.check_skill(...)`
  (raising `PolicyViolationError` on deny) and emits the governance shadow
  check (`GateKind.SKILL` with `side_effects` + `skill_name` attributes —
  `governance/adapters.py::SkillGate`) **before** `dispatch_skill`. Natively
  the policy comes from the same project config the runner's wrapped Executor
  was built with; on Temporal it is already threaded —
  `ActivityContext.skill_policy` exists and is populated by
  `configure_activities` (made real for the durable path by #822) but
  `call_skill_activity` **never reads it today**. The tool-node change closes
  that hole for the activity path as a side effect.

This is deliberately the runtime re-check pattern the Executor documents
("bundles loaded over HTTP can skip validate, so we re-check here") applied to
the one skill entry point that currently lacks it.

### D6 — Determinism lint and state-threading lint cover tool nodes via metadata

The Temporal compiler's existing lint (`compilers/temporal.py::lint`, ~L323)
already warns on any node whose `metadata["capabilities"]` carries
`deterministic: false`. Because D2 stamps the skill's `capabilities` block into
the node's metadata at compile time, a nondeterministic skill behind a tool
node is linted **with zero lint changes** — the hook was built for exactly
this.

`mdk validate`'s state-threading lint (`cli/validate.py::_lint_state_threading`)
gets a tool-node arm like the decision-node arm ADR 094 added: a tool node
*consumes* its input-map source paths (or the skill input schema's `required`
keys under default projection) and *produces* its output schema's `properties`
keys (namespaced under `output_key` when set) — so "this skill's required input
is produced by nothing upstream" fails review, not production.

### D7 — Observability: a `workflow.tool` span, no synthetic RunRecord

A tool node runs no model, so it produces **no `RunRecord`** — we do not forge
one. (`SkillCallRecord` is also out: it is a per-turn entry inside an *agent's*
`RunRecord.skill_calls`, meaningless without a parent run.) This is consistent
with what `call_skill_activity` persists today: nothing. Instead, mirroring the
decision node (ADR 094 D4):

- **Native:** `_run_tool` opens a `workflow.tool` span nested under the
  workflow root, attributed with `workflow.node_id`, `tool.skill` (name),
  `tool.side_effects`, outcome, and on failure the `SkillErrorType` — visible
  in Langfuse/Grafana and exported as a span row by the ADR 095 pipeline.
- **Temporal:** the activity invocation is durably recorded in workflow
  history (scheduling, attempts, result), and the activity opens the same
  `workflow.tool` span via the tracer in its `SkillExecutionContext` — so both
  backends emit the same trace shape.

The workflow-level business outcome is unchanged: the skill's contribution to
`final_state` lands in the `WorkflowRunRecord` like every other node's.

## Alternatives considered

- **Status quo: wrap the skill in a single-purpose agent.** Rejected — that is
  gap #50 itself: an LLM turn's cost and nondeterminism for a function call,
  and the side-effect hidden from the authoring surface.
- **A `function` node (inline Python in `workflow.yaml`).** Rejected —
  arbitrary inline code has no schema contract, no `side_effects` declaration,
  no registry, and would bypass the SKILL gate by construction.
  `NodeType.FUNCTION` stays reserved.
- **`skill:` as a path (like agent `ref:`).** Rejected — agents reference
  skills by registry name; two vocabularies for the same object would be a
  gratuitous inconsistency, and a name keeps `workflow.yaml` portable across
  checkouts. The *compiler* still produces a path (`node.ref`), preserving the
  `call_skill_activity` contract unchanged.
- **Run-time name resolution.** Rejected — a typo'd skill name must fail
  `mdk validate`/compile, not the Nth production run (the agent-ref rule).
- **Pass-whole-state input by default.** Rejected — the schema projection is
  the established rule for agents *and* for `call_skill_activity`; whole-state
  would be a behavioral fork between agent-invoked and node-invoked skills.
- **Always-namespaced output (mandatory `output_key`).** Rejected as default —
  raw merge is the agent-node convention and what the emitted Temporal code
  already does; mandatory namespacing would make the tool node the one node
  type whose output a downstream agent's projection cannot see without
  re-mapping. `output_key` keeps collision safety one line away.

## Boundary (out of scope)

The tool node executes **one registered skill, synchronously, as one step**. It
is not a sub-workflow (`NodeType.SUB_WORKFLOW`, reserved), not inline code
(`FUNCTION`, reserved), not a fan-out body (parallel branches stay agent-only
per ADR 092 Phase 1), and not an MCP-server lifecycle manager (the `mcp` skill
kind already owns that). Per-node retry/timeout overrides remain the ADR 054
Phase 3 surface.

## Consequences

- **Purely additive (CLAUDE.md rule 5).** A `ToolNodeSpec` joins the
  discriminated union (every existing spec keeps `extra="forbid"`);
  `validate_linear` admits `NodeType.TOOL`; the native runner gains a dispatch
  arm; `_emit_skill_node` gains two emitted args; `call_skill_activity` gains
  two defaulted trailing parameters. No change to `agent.yaml` /
  `project.yaml` / `skill.yaml` schemas, the `/api/v1` runtime API, storage
  schema, CLI flags, or env vars. Existing workflows compile byte-for-byte
  unchanged; the existing 4-arg activity call keeps its exact semantics.
- **Native ≡ Temporal by shared code path**, the house rule: both funnel
  through `dispatch_skill` plus the new pure `core/workflow/tool.py` helpers
  (the `decision.py`/`judge.py` precedent).
- **Boundary rules respected:** `core/workflow` keeps depending on the
  `SkillBackend` Protocol surface (`dispatch_skill`), never a concrete
  backend; tracing stays wired at the edges (runner / activity), not inside
  `tool.py`; `temporalio` stays behind the existing lazy imports.
- **Two latent gaps in `call_skill_activity` get closed in passing:** it
  starts honoring `timeout_call_ms`, and it starts enforcing
  `ActivityContext.skill_policy` + the governance SKILL shadow (it checks
  neither today).
- **Deploy note (worker/API image drift):** the activity-side changes execute
  on the **worker** image; a drifted API-only rollout would run tool nodes
  with the old ungoverned activity. Same operational rule as ADR 060 D4.
- Authors get the deterministic external-call primitive directly:
  `fetch → decide → act` workflows (lookup skill → decision node → agent)
  with no LLM on the deterministic hops, each external call declared,
  policy-gated, linted, and visible in `workflow.yaml`.
