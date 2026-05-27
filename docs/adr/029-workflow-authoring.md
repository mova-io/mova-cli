# ADR 029 — Workflow authoring: the copilot + `mdk dev` understand workflows

**Status:** Accepted
**Date:** 2026-05-27 (proposed); 2026-05-27 (approved)
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x authoring DX — the workflow engine (ADR 017) is
powerful but only reachable by hand-writing `workflow.yaml`; make composing,
testing, and observing a multi-step pipeline as guided as authoring a single
agent.
**Builds on / related:** ADR 025 (authoring catalog/driver/planner + MCP server
+ `cli/dev_cmd.py`), ADR 017 (`WorkflowRunner`, `WorkflowSpec`, node types), ADR
008 (workflow-level evals), ADR 024 (per-step spans), ADR 027 (the live loop),
ADR 028 (the `workflow` starter template). **Absorbs** the held D7d
("project-level multi-agent scaffolding via the copilot").

## Context

`WorkflowSpec` (`core/workflow/spec.py`) is already a first-class declarative
entity: `nodes` (`agent`, `intent-router`, `human`), `edges`, `state_schema`,
`entrypoint`, and workflow-level `evals`. `WorkflowRunner` executes it; ADR 024
gives per-node spans. But the **ADR 025 authoring spine only understands single
agents** — the catalog's typed actions (add-context, edit-instructions,
add-eval-case, …) operate on an `agent.yaml`, and `mdk dev` drives a single
agent. Composing a pipeline (add a step, route on intent, add a human approval
gate, wire edges) is hand-YAML with no guided, verified path. That is the gap
between "we have an orchestration engine" and "the team can actually build
workflows."

## Decision

Extend the authoring spine to a **`workflow` entity**, mirroring exactly how
single-agent authoring works — same catalog → planner → driver
(plan → preview → confirm → apply → verify → undo) spine, one level up.

### D1 — New catalog actions over `workflow.yaml`
Add typed `AuthoringAction`s (each with an `args_model`, so the planner and the
MCP server pick them up automatically — the surfaces are catalog-driven):
`scaffold-workflow`, `add-agent-node`, `add-intent-router`, `add-human-gate`,
`connect-edge`, `set-entrypoint`. Each mutates `workflow.yaml` through the
**existing** `AuthoringDriver`, so confirmation-gating, snapshot/undo, and the
verify step all hold transitively. The actions never edit YAML directly — they
go through the driver like every other action.

### D2 — `mdk dev <workflow>` understands a workflow target
Point `mdk dev` at a `workflow.yaml` (resolved by name/path per ADR 026) and it:
loads + **visualizes the node graph** (nodes + edges + entrypoint), **runs** it
via the existing `WorkflowRunner`, and surfaces the **per-node trace** (ADR 024
spans) so a multi-step run is debuggable. The ADR 027 live loop applies here too
— edit a node's agent or the workflow shape, re-run, see the new path.

### D3 — The planner authors workflows in natural language
The existing `Planner` (ADR 025) maps an NL request over the **expanded** catalog
to typed workflow actions — e.g. *"add a triage step that routes billing vs.
technical to two agents, with a human approval before any refund"* → a confirm-
gated sequence of `add-intent-router` + `add-agent-node` + `add-human-gate` +
`connect-edge`. Ambiguous → clarification (it never guesses), same as today.

### D4 — `verify` for workflows
The driver's verify step, for a workflow, = **load + validate** the spec
(node-id uniqueness, every edge endpoint exists, entrypoint exists, no dangling
nodes, cycle sanity) + a **`--mock` run** through `WorkflowRunner` + (if an evals
block is present) the **ADR 008 workflow eval gate**. So every applied workflow
edit is checked before it sticks; a failing verify reverts via the driver's undo.

## Consequences

**Positive**
- The orchestration engine becomes usable by the whole team — workflows are
  composed/tested/observed with the same guided spine as single agents.
- Reuses ADR 025's driver/planner/MCP + ADR 017's runner + ADR 024's spans + ADR
  008's evals — **no new execution or eval engine**.
- The MCP server + AGENTS.md (ADR 025) gain workflow fluency for free, because
  the surfaces are catalog-driven.

**Negative / risks**
- Workflow integrity in `verify` (D4) — cycle detection, edge/entrypoint
  validity — is the main correctness surface; keep those checks tight and
  well-tested.
- The catalog grows a second entity type; keep agent vs. workflow actions clearly
  namespaced so the planner's prompt stays unambiguous.

## Boundaries
Pure orchestration over the existing `WorkflowRunner` + catalog driver — `cli ⊥
runtime` holds; no new engine, no new dependency. New surface is typed actions +
a `mdk dev` workflow mode.

## Scope / rollout
Likely **2 PRs**: (1) the workflow catalog actions + driver verify; (2) the
`mdk dev` workflow mode (graph view + run + trace). Sequence **after** ADR 027
(reuses the live loop) and ADR 028 (the `workflow_init` starter is what these
actions edit/extend). The capstone of the authoring-DX arc.

## Alternatives considered
- **A separate workflow-only tool / DSL.** Rejected: splits the authoring story;
  the catalog/driver/planner spine already generalizes to a second entity.
- **Adopt an external graph framework's authoring UX.** Rejected per ADR 017 /
  the minimal-deps rule — `WorkflowSpec` + the runner already exist; this is
  authoring over them, not a new engine.
