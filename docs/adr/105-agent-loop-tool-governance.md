# ADR 105 — Agent-loop tool governance: a confirm/HITL gate for mutating tools

Status: Accepted
Date: 2026-06-13
Accepted: 2026-06-13 — approved by Jeremy. Shipped: D1 (mutating→side_effects in the bridge), D2 (`confirm_side_effects` on SkillPolicy), D3 sync (interactive `mdk run`/`dev` approval callback) + headless fail-closed, D6 (default-off). The confirm check lives in the executor (which owns the policy) rather than threaded through `SkillExecutionContext` — equivalent, no Protocol change. Deferred: D3b (ADR 077/089 durable approval for headless), D4 agent-level tightening, richer D5 records.
Deciders: Engineering — extend the existing `SkillPolicy` gate (don't add a
second one) with a `confirm` posture + an approval seam, and make the mutating
label actually reach the gate. (CLAUDE.md §7: extend the existing seam.)
Builds on: ADR 002 (skills + `dispatch_skill`; `SkillSideEffects`), ADR 093
(governance gates — the `GateKind.SKILL` shadow), ADR 097 D5
(`govern_skill_dispatch` — the workflow-side skill gate), ADR 077/089 (HITL
signal + pause/resume — the durable-approval machinery for workflows), ADR 101
(MCP discovery; `ToolGovernance.mutating` + the MCP `destructiveHint`/
`readOnlyHint` mapping).

## Context

An agent's tool-use loop can call a tool that **changes the world** — and today
nothing stops it from doing so autonomously. MCP makes this urgent: a few lines
of `mcp_servers:` (ADR 101) can hand an agent a GitHub `delete_*`, a Slack post,
or a DB write, and the model decides when to call them.

What exists today (verified):

- **A deny gate.** `SkillPolicy` (`core/config.py:264`) gates skills by
  `side_effects` category (`read-only` / `network` / `filesystem` /
  `mutates-state`). It's enforced in the agent loop at `Executor` entry
  (`core/executor.py:291-294`, `:449-462`) and on the workflow side via
  `govern_skill_dispatch` (`core/executor.py:251`, ADR 097 D5). Plus the ADR 093
  governance *shadow* (`GateKind.SKILL`, warn-mode, records but never blocks).

So a gate exists — but it has **three gaps** that leave mutating tools effectively
ungoverned in practice:

1. **Allow/deny only — no `confirm`.** `SkillPolicy.allowed_side_effects` is an
   allowlist: a category is either permitted for the whole project or rejected
   outright (the skill won't validate / won't run). There is no middle ground —
   *"allow this write tool, but get a human's OK at call time."* In reality
   operators want exactly that for write tools, so they end up setting the policy
   permissive (the default) and the gate does nothing.

2. **The mutating label doesn't reach the gate for MCP tools.** ADR 101 maps an
   MCP tool's `destructiveHint`/`readOnlyHint` → `ToolGovernance.mutating` on the
   minted descriptor (surfaced in `mdk mcp inspect`). But
   `tool_descriptor_to_skill_bundle` (`core/tool_registry/bridge.py`) mints the
   `SkillSpec` with the **default** `side_effects` (`read-only`) and never
   translates `governance.mutating`. So even a strict
   `SkillPolicy: [read-only]` would wave a discovered GitHub `delete_repository`
   through as read-only. The label exists; the gate can't see it.

3. **No approval mechanism for a running agent.** Even with a `confirm` posture,
   *how* does a human approve mid-turn? Workflows have the ADR 077/089 HITL
   signal + pause/resume; the conversational agent loop has nothing — it's one
   synchronous request→response.

For internal/dogfood use the permissive default is acceptable (it's why the MVP
can ship). Before an **autonomous, write-capable** agent runs against real
systems, these gaps are a real operational risk and should be closed.

## Decision

Extend the one existing gate; add a `confirm` posture and a pluggable approval
seam; default to today's behavior so nothing breaks.

### D1 — Make the mutating label reach the gate (the cheap, high-value fix)

`tool_descriptor_to_skill_bundle` maps `ToolGovernance.mutating=True` (and the
MCP `destructiveHint`/non-`readOnlyHint` signal ADR 101 already computes) →
`SkillSpec.side_effects = MUTATES_STATE`. Then a discovered mutating MCP tool is
**visible to the existing `SkillPolicy`** with no new gate. This is small and
self-contained; it could ship ahead of the rest of this ADR.

Behavior change (flagged): a project that already sets a non-permissive
`SkillPolicy` excluding `mutates-state` will now correctly **block** discovered
mutating MCP tools that previously slipped through. Permissive projects (the
default) see no change.

### D2 — A `confirm` posture on `SkillPolicy` (allow | deny | confirm)

Add an optional `confirm_side_effects: list[SkillSideEffects]` to `SkillPolicy`
(default `None` → today's behavior). Semantics per category:

- in `allowed_side_effects` → **allow** (unchanged).
- in `confirm_side_effects` → **confirm**: the call requires approval before
  dispatch (D3); on denial it returns a `SkillError` the LLM sees as a normal
  tool result (*"the user declined this action"*), so the agent can adapt rather
  than crash.
- neither → **deny** (unchanged: `PolicyViolationError`).

`confirm` is the missing middle: keep the tool available, gate the side effect.
A typical write-capable agent: `allowed_side_effects: [read-only, network]`,
`confirm_side_effects: [mutates-state, filesystem]`.

### D3 — A pluggable approval seam (sync now; durable deferred)

Thread an optional `approve: ApprovalCallback | None` through
`SkillExecutionContext` (the existing side-channel, `skill_backend/base.py`).
The executor, before dispatching a `confirm`-category tool, calls `approve(skill,
input)` → bool.

- **Interactive (`mdk run`, `mdk dev`):** the CLI supplies a callback that prints
  the tool + arguments and prompts the operator (y/N). The "human at a keyboard"
  case — the common one for authoring/dogfood.
- **Headless / runtime API:** no interactive console. Two options, smallest
  first: (a) **default-deny** confirm-category tools (safe: a write needs an
  approver, and none is wired → it's declined with a clear message); (b) wire an
  approval channel by **reusing the ADR 077/089 HITL pause/resume** — the run
  emits an `approval_required` event and suspends until a signal arrives. (b) is
  the richer path and is where agent + workflow HITL converge; this ADR commits
  to (a) as the headless default and scopes (b) as the follow-on (see Boundary).

The callback returning `False`/absent is always safe (the action doesn't happen);
the gate fails closed.

### D4 — Config surface + precedence

`confirm_side_effects` lives on the project `SkillPolicy` (the existing block);
an agent may *tighten* (not loosen) via an `agent.yaml` override, mirroring how
`ModelPolicy`/`RuntimePolicy` compose. One gate, one mental model. No new
top-level stanza.

### D5 — Observability

Each gated decision (allow/confirm-approved/confirm-denied/deny) emits a
`GateKind.SKILL` governance record (ADR 093) with the outcome + `side_effects` +
`skill_name`, so operators can audit what an agent was allowed to do and what a
human approved. Reuses the existing shadow path, promoted from warn-only to
record-the-decision.

### D6 — Compatibility: default-off

- `confirm_side_effects=None` (default) → the gate behaves exactly as today.
- D1's mapping only changes behavior for projects that *already* run a
  non-permissive `SkillPolicy` (they get the correct, stricter result).
- No change to `dispatch_skill`, the `SkillBackend` Protocol, or the MCP
  backends; the approval check sits in the executor before dispatch, and the
  callback defaults to `None` (allow for non-confirm categories).

## Boundary (out of scope)

- **Durable, long-horizon approvals for headless agents** (an approval that
  arrives minutes/hours later, surviving process restarts) — that is the ADR
  077/089 workflow HITL path; this ADR's headless default is fail-closed deny,
  with the pause/resume integration as a named follow-on (D3b).
- **Per-tool / per-argument ACLs** (e.g. "allow `merge_pull_request` only on
  repo X") — a finer policy than side-effects categories; revisit if a scenario
  needs it.
- **MCP resources/prompts** — unrelated; ADR 101 is tools-only.

## Alternatives considered

- **Rely on `SkillPolicy` deny only (status quo).** Too blunt: denying
  `mutates-state` makes write tools unusable; allowing it gates nothing. Operators
  default to permissive, so mutating tools run ungoverned. `confirm` is the
  posture the real use case needs.
- **A new, separate tool-approval gate.** Rejected: a second gate alongside
  `SkillPolicy` splits the mental model and the enforcement points. Extend the
  one gate.
- **LLM self-judges whether to ask.** Rejected: non-deterministic on a
  safety-critical path; the model deciding when *it* needs approval defeats the
  purpose.
- **Block at `mdk validate` only.** Insufficient: a bundle loaded over HTTP can
  skip validate (the very reason `SkillPolicy` re-checks at `Executor` entry);
  approval must be enforced at dispatch.

## Consequences

- An operator can run a write-capable agent safely: read/lookup tools flow
  freely, mutating tools (including discovered MCP write tools, via D1) require an
  explicit OK — interactively today, fail-closed headless, durable-approval next.
- The MCP mutating labels (ADR 101) stop being decorative: D1 wires them into the
  gate that already exists.
- Default behavior is unchanged, so the MVP can ship now and adopt this when
  autonomous write access is actually turned on.
- Estimated scope: ~2 PRs — (1) D1 (bridge mutating→side_effects) +
  `confirm_side_effects` on `SkillPolicy` + the executor confirm check + the
  `mdk run`/`dev` interactive callback + headless fail-closed; (2) the ADR
  077/089 pause/resume approval channel for headless runs (D3b). D1 alone is a
  small, shippable correctness fix.
