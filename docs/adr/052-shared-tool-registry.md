# ADR 052 — Shared tool registry: a control-plane catalog of tool *descriptors*, with execution staying in the runtime's `SkillBackend`

**Status:** Proposed
**Date:** 2026-05-30
**Deciders:** Engineering + Deva (Movate)
**Builds on / composes with (changes nothing in any of them):**
ADR 002 (skills + contexts — **the `SkillBackend` Protocol** in
`src/movate/core/skill_backend/` and the five-value `SkillError` taxonomy;
*this ADR is a control-plane catalog over those backends — it adds a new
`exec` backend but does not alter the dispatch contract, the JSON-in/JSON-out
shape, or the error vocabulary*),
ADR 041 (agent catalog — the four-tier `source` model
movate-curated / private / community + the `StorageProvider`-backed catalog
schema + sync; **the tool registry reuses that exact tiering and resolution
posture, applied to tools rather than agents**),
ADR 018 (per-tenant provider keys / BYOK — tool credentials are stored
**by reference** and resolved per-tenant through the same seam, never inlined
into a descriptor),
ADR 036 (usage metering + quotas — each tool call meters as a normal skill
call against the per-run cost surface; no new meter),
ADR 013 (flat least-privilege scopes — `/api/v1/tools` declares `read` /
`write` / `admin` against the existing scope vocabulary),
ADR 017 D5 (durable + HITL on the native runner — the stubbed-then-shipped
`HUMAN` node is the gate a `mutating` tool routes through),
ADR 032 / item #147 (front-end API completion — the `/api/v1` compat contract
+ the OpenAPI contract test that enforces CLI↔API parity),
ADR 019 / ADR 024 (trace-context propagation + step observability — a tool call
is a `tool_call` span, unchanged).

**Defining architectural principle.** **The registry is a *control-plane
catalog of descriptors*. It never executes a tool.** A descriptor names a tool,
its version, its I/O JSON-Schema, and *which backend reaches it* — but the
actual call still happens in the runtime, through the **existing
`SkillBackend` dispatch** (`dispatch_skill` in
`src/movate/core/skill_backend/base.py`). The registry is to tools what ADR 041
is to agents: a **package manager**, not a runtime. Resolution is a build-time /
load-time step that turns a `name@version` reference into a concrete
`SkillSpec`; from there the runtime is unchanged. This is the same control-plane
(`cli`) ⊥ execution-plane (`runtime`) boundary CLAUDE.md rule 6 requires.

## Context

Two gaps block the reuse story Deva wants:

1. **Skills are per-agent — there is no sharing.** Today a skill (an MCP tool,
   an HTTP endpoint, a Python entrypoint) is declared *inside* one agent's
   bundle and loaded by that agent's `SkillBundle` loader. Two agents that both
   need "create a Jira ticket" each redeclare it; there is no tenant-level
   library, no single place to version it, govern it, or point a new agent at
   it. The ADR-041 catalog made *agents* shareable; tools were left behind.

2. **There is no language-agnostic script runner.** The four shipped backends
   are Python (`pkg.mod:func`), HTTP (REST), MCP (JSON-RPC over stdio), and
   agent (agent-as-tool). A team with an existing Node CLI, a Java jar, or a Go
   binary that already does the work has no first-class way to wrap it as a
   tool — short of hand-writing a Python shim or standing up an HTTP service.

Deva's ask is a **common tool set** that (a) spans multiple languages
(Python / Node / Java / any executable), (b) reaches SaaS systems (Jira,
ServiceNow) out of the box, and (c) is **shareable across agents** at the
tenant / org level — the same "browse, pull, depend-on" ergonomics ADR 041
gave agents, now for tools.

This ADR records the **architecture** of that registry (the descriptor model,
the resolution algorithm, the three reach mechanisms, the governance seams,
the API/CLI surface) — not the connector-pack contents, the UI, or the rollout
schedule.

## Decision

A tenant/org-scoped **registry of typed `ToolDescriptor`s**, resolved by
`name@version` and tiered exactly like the ADR-041 catalog. Agents *depend on*
tools by reference; a `ToolResolver` late-binds each reference to a concrete
`SkillSpec` at load time; the runtime executes it through the unchanged
`SkillBackend` dispatch. The descriptor hides the implementation behind the
normalized JSON-in / JSON-out contract that already governs every skill.

### D1 — The model: a package manager for agent tools

A `ToolDescriptor` is the unit of the registry — a *typed, versioned,
governed pointer* to a capability:

```jsonc
{
  "name": "jira.create_issue",
  "version": "1.2.0",                  // semver — D4 enforces compat on bumps
  "scope": "tenant",                   // movate | tenant | project | community
  "title": "Create a Jira issue",
  "description": "Open a new issue in a Jira project.",
  "input_schema":  { /* JSON Schema — the model contract, in */ },
  "output_schema": { /* JSON Schema — the model contract, out */ },
  "backend": {                         // D2 — how the runtime reaches it
    "kind": "mcp",                     // mcp | exec | http
    "config": { "entry": "npx -y @movate/mcp-jira", "tool": "create_issue" }
  },
  "credentials_ref": "tenant:jira-cloud",   // BYOK by reference (ADR 018) — never inlined
  "governance": {
    "mutating": true,                  // → HITL gate at resolve/run (ADR 017 D5)
    "allowlist_required": true         // must be on the agent/tenant allowlist (D6)
  }
}
```

Agents depend on tools the way a package manifest depends on libraries:

```yaml
# agent.yaml
tools:
  - jira.create_issue@1.2.0
  - servicenow.lookup_incident@^2.0.0   # version constraint, npm-style
```

The reference is **late-bound**: the agent records `name@version` (or a
constraint), and the `ToolResolver` materializes the concrete `SkillSpec` at
load time. The implementation (`mcp` / `exec` / `http`) is **hidden behind the
descriptor** — an agent author depending on `jira.create_issue@1.2.0` neither
knows nor cares whether it is reached via MCP today or a curated HTTP connector
tomorrow. Swapping the backend (with a compatible schema) is a non-breaking
descriptor change. This is the decouple pillar (D4) in one sentence: *agents
bind to a normalized contract, not to an implementation.*

### D2 — Three reach mechanisms

A descriptor's `backend.kind` selects one of three ways to reach the tool. All
three are `SkillBackend` implementations behind the same JSON-in/JSON-out
contract; the registry only chooses *which*.

1. **`mcp` (the default).** Language-agnostic by construction — an MCP server
   can be written in anything and the SaaS ecosystem is converging on MCP. This
   is the reach mechanism for the curated connector pack (D5) and the recommended
   path for any new shared tool. Uses the **existing** `MCPSkillBackend`
   (`src/movate/core/skill_backend/mcp.py`) unchanged.

2. **`exec` / `command` (NEW backend — the raw-script escape hatch).** Run *any*
   executable with the normalized contract: the tool's JSON input is delivered
   on `stdin` (and/or templated argv), the process writes a JSON object to
   `stdout`, exit-code + `stderr` map to the `SkillError` taxonomy. This is how
   a Node CLI, a Java jar (`java -jar tool.jar`), a Go binary, or a bare Python
   script becomes a tool **without** an MCP server or an HTTP service. It runs
   **sandboxed** (see Boundaries — resource limits, no implicit network, a
   working dir, env allowlist). This is the genuinely-new surface that closes
   the "no language-agnostic script runner" gap; see Surfaces (D7) for the
   contract.

3. **`http` (REST).** For tools that are already an authenticated REST endpoint.
   Uses the **existing** `HttpSkillBackend` (`src/movate/core/skill_backend/http.py`)
   unchanged.

The Python and agent backends are *not* listed here: a registry tool is a
shareable, language-agnostic artifact, and a `pkg.mod:func` Python entrypoint
is inherently bound to the agent's own image. Python skills remain a per-agent
mechanism (status quo). The registry is for tools that *cross* agents.

### D3 — The shared registry (tenant/org-level, ADR-041 tiering)

The registry is **tenant/org-scoped** and tiered exactly like the ADR-041
agent catalog, distinguished by a `scope` (the analogue of ADR 041's `source`
column):

| Tier | Who owns it | Where it lives |
| ---- | ----------- | -------------- |
| `movate` | Movate-curated (the connector pack, D5) | published like ADR 041's `movate/agent-catalog`; cached in customer Postgres |
| `tenant` | the customer's own org-wide tools | customer Postgres, never synced upward |
| `project` | a single project's tools | customer Postgres, `project_id` set |
| `community` | future, moderated | schema-ready, writes blocked (mirrors ADR 041 D7) |

Agents reference a tool by `name`; the **resolver walks scope precedence**
`project → tenant → movate` (most-specific wins — a project can shadow a
tenant tool, a tenant can shadow a movate tool), applies the **version
constraint** from the `agent.yaml` reference, and checks the **allowlist**
(D6) before binding. Persistence lives **behind the `StorageProvider`
Protocol** (CLAUDE.md rule 6): Postgres in production, SQLite for local
dev/test — the same dual-backend posture as ADR 041. The schema is the
ADR-041 `catalog_entries` pattern re-applied: `tool_descriptors` +
`tool_descriptor_versions`, keyed `(name, scope, tenant_id)` /
`(name, version, scope, tenant_id)`.

### D4 — Pillar 1: Decouple

- **Descriptors, not implementations.** An agent's manifest names
  `name@version`; the impl is hidden. A tool can move from `exec` to `mcp` to a
  curated `http` connector with no agent change as long as the I/O schema holds.
- **Late binding.** Resolution happens at load time, not author time — the
  agent's manifest is a *constraint*, resolved against whatever versions the
  tenant has, under scope precedence.
- **Semver + schema-compat enforcement on bumps.** Publishing a new version
  runs a compatibility check against the prior version's schemas: a backward-
  compatible change (added optional input field, widened output) is a MINOR/PATCH
  bump; a breaking change (removed/renamed field, narrowed type, new required
  input) **requires a MAJOR** bump and is rejected at `publish` otherwise. This
  is the contract that makes `^`-style constraints safe and protects the CLAUDE.md
  rule-5 compat posture for anything an agent depends on.

### D5 — Pillar 2: Reach

The three backends of D2, plus a **Movate-curated connector pack** shipped in
the `movate` tier: Jira and ServiceNow as MCP connectors (the two SaaS systems
named in the ask), each a `ToolDescriptor` with a published I/O schema and a
`credentials_ref` the tenant fills with its own credentials (D6 / ADR 018).
The connector pack is published with the same reviewed-PR + smoke-eval gate
ADR 041 D3 uses for curated agents.

### D6 — Pillar 3: Govern

Governance is enforced **at resolve and at run**, reusing existing seams:

- **Allowlists.** Per-agent and per-tenant allowlists are checked when the
  resolver binds a reference. A tool not on the applicable allowlist fails
  resolution with a clear error — an agent cannot silently acquire reach it was
  not granted. `governance.allowlist_required` makes this mandatory for a tool;
  tenants may also run allowlist-by-default.
- **`mutating` → HITL.** A descriptor flagged `mutating: true` (anything that
  writes to a system of record — create a Jira ticket, close a ServiceNow
  incident) routes its call through the **`HUMAN` node** (ADR 017 D5) for
  approval before execution. Read-only tools run straight through.
- **Credentials by reference (BYOK).** `credentials_ref` names a per-tenant
  credential resolved through the ADR-018 seam at execution time. Credentials
  are **never** inlined into a descriptor (descriptors are shareable artifacts;
  secrets are not). The descriptor crosses tenant boundaries; the secret does
  not.
- **Audit + metering per call.** Every tool call is a normal skill dispatch, so
  it already emits a `tool_call` span (ADR 019/024) and meters against the
  per-run cost surface (ADR 036). The registry adds *which descriptor + version
  + scope* resolved, for audit — no new metering plumbing.

### D7 — Surfaces (NEW)

- **API: `/api/v1/tools`** on the customer runtime (ADR 032 compat contract;
  scopes per ADR 013):

  | Method | Path | Scope | Purpose |
  | ------ | ---- | ----- | ------- |
  | `GET`  | `/api/v1/tools?scope=&q=&tag=` | `read`  | List/search; resolver precedence + allowlist applied |
  | `GET`  | `/api/v1/tools/{name}`         | `read`  | Descriptor detail (latest + summary) |
  | `GET`  | `/api/v1/tools/{name}/versions`| `read`  | Version history |
  | `POST` | `/api/v1/tools`                | `admin` | Publish a tenant/project descriptor (compat check, D4) |
  | `POST` | `/api/v1/tools/{name}/publish` | `admin` | Promote a draft version to latest |

- **CLI: `mdk tools`** — `list` / `search` / `add` / `publish`, mirroring the
  API one-to-one. `mdk tools add <name@version>` records the dependency in
  `agent.yaml`; `mdk tools publish ./tool.yaml` runs the compat check and
  writes a descriptor.

- **`agent.yaml`:** a new optional `tools: [name@version]` block (D1). This is
  an **additive** schema change — existing per-agent `skills:` declarations are
  untouched and continue to work (a registry tool resolves *into* the same
  `SkillSpec` shape the executor already consumes).

- **The `exec` backend contract (NEW):** JSON input on `stdin`; a single JSON
  object on `stdout` is the result; non-zero exit → `backend_error` with the
  `stderr` tail; wall-clock overrun → `timeout`; non-JSON / non-object stdout →
  `validation_failed`. Sandbox: explicit working dir, env allowlist, resource
  limits, no implicit network. This maps 1:1 onto the five `SkillError` values
  the dispatch contract already defines.

- **CLI↔API parity** is enforced by the existing OpenAPI contract test
  (ADR 032 / `tests/test_front_end_api_contract.py`): every `mdk tools` verb
  maps to a `/api/v1/tools` endpoint.

### D8 — Exactly four genuinely-new pieces; everything else is a reused seam

To keep the blast radius legible, state the split explicitly.

**NEW (the only new construction):**
1. **`ToolDescriptor` + `ToolResolver`** — a new `core/tool_registry/` module:
   the descriptor type, the scope-precedence + version-constraint + allowlist
   resolution that turns `name@version` into a concrete `SkillSpec`.
2. **The `exec` / `command` `SkillBackend`** — a new sibling in
   `src/movate/core/skill_backend/` (the raw-script escape hatch, D2.2).
3. **Tools storage + migration** — `tool_descriptors` +
   `tool_descriptor_versions` behind the `StorageProvider` Protocol (Postgres +
   SQLite), with a migration.
4. **`/api/v1/tools` + `mdk tools`** — the API endpoints and CLI verbs (D7).

**REUSED (no new construction — the leverage):**
- `SkillBackend` dispatch + the `SkillError` taxonomy (ADR 002) — a resolved
  tool *is* a skill call.
- `StorageProvider` Protocol (persistence) — same dual-backend posture as ADR 041.
- ADR-041 tiering + resolution posture (movate/tenant/project/community, scope
  precedence, the curated-PR publish gate).
- Credentials / BYOK by reference (ADR 018).
- Tracing + metering (ADR 019/024/036) — the `tool_call` span + per-run cost.
- The `HUMAN` / HITL node (ADR 017 D5) — the gate for `mutating` tools.

The leverage ratio (four new pieces, six reused seams) is the point: this is a
catalog *over* machinery that already exists, not a parallel runtime.

### Phase-1 cut

Ship the smallest coherent slice that proves the model:

- `ToolDescriptor` + `ToolResolver` for the **`tenant` and `project`** scopes
  (defer the `movate` sync mechanics + `community` to a follow-up — schema-ready
  from day one, mirroring ADR 041 D7);
- the **`exec` backend**;
- **one** curated connector (Jira **or** ServiceNow via MCP) as the first
  `movate`-tier descriptor, to validate the connector-pack shape;
- **name resolution** (scope precedence + version constraint) and
  **allowlists**.

Deferred to follow-ups: the `movate`-tier sync protocol (ADR 041 D4 watermark
sync re-applied), community writes, the full Jira+ServiceNow pack, and richer
`exec` sandbox policy knobs.

## Consequences

**Positive**
- One shared, versioned, governed tool library spanning agents — the reuse gap
  closes for tools the way ADR 041 closed it for agents.
- A language-agnostic path (`exec`) for any existing Node/Java/Go/Python
  executable, with no MCP server or HTTP service required.
- Agents decouple from tool implementations: a tool can be re-backed
  (exec → mcp → curated http) with no agent change, as long as the schema holds.
- Governance is uniform and reuses shipped seams: allowlists at resolve, HITL
  for mutating tools, BYOK-by-reference, audit + metering per call.
- Tiny new surface area relative to capability (D8): four new pieces, six
  reused seams.

**Negative / risks**
- **The `exec` backend runs arbitrary executables** — sandboxing is
  security-critical and must be real (resource limits, env allowlist, no
  implicit network, no shell-injection via argv templating). A weak sandbox is
  a tenant-isolation hole. This is the single highest-risk new piece and gates
  on a security review (Deva sign-off) before it ships.
- **Two authorities for "what a tool is"** (the descriptor in the registry vs.
  the resolved `SkillSpec` the runtime loads). Mitigation: resolution is the
  one-way binding step (descriptor → `SkillSpec`); the runtime never reads the
  registry mid-run, exactly as ADR 041 keeps catalog ⊥ deployed-registry.
- **Schema-compat enforcement is only as good as its check.** A too-loose check
  lets a breaking change ship as a MINOR and breaks every `^`-pinned agent.
  Mitigation: conservative compat rules (any removed/renamed/narrowed field =
  MAJOR) and the publish gate runs them.
- **MCP transport gap** (see Boundaries): the curated connector pack assumes
  remote SaaS MCP servers, but `MCPSkillBackend` is **stdio-only** today.

## Alternatives considered

- **MCP-server-registry only** (just catalog MCP server endpoints; no
  descriptor abstraction, no `exec`, no `http`). Rejected: it forces *every*
  shared tool to be an MCP server (excludes the raw-script and REST cases Deva
  named), leaks the implementation into the agent (no late-binding / re-backing),
  and has nowhere to hang governance (allowlist, mutating-flag, credentials-ref)
  except per-server. The descriptor *is* the value; the MCP server is one
  backend behind it.
- **Per-agent skills (status quo).** Rejected — it *is* the problem: no sharing,
  no central versioning, no tenant library, no language-agnostic runner.
- **A code SDK of tools** (ship a Python package of tool functions agents
  import). Rejected: Python-only (defeats the multi-language ask), couples every
  consumer to the SDK's release cadence and the agent's image, and offers no
  late-binding, no per-tenant governance, no BYOK-by-reference. The registry is
  data (descriptors) precisely so it can be governed and tiered, not code.

## Boundaries (out of scope)

- **Implementation is a separate effort.** This ADR is **docs-only**; the
  `core/tool_registry/` module, the `exec` backend, the storage migration, and
  the `/api/v1/tools` surface are built under their own PR(s) against this
  decision.
- **MCP HTTP/SSE transport is a noted dependency.** `MCPSkillBackend` is
  **stdio-only** today (its own scope note). Curated SaaS connectors that run as
  remote MCP servers need HTTP/SSE transport in that backend first; until then
  the connector pack ships as stdio bridges (e.g. an `npx`-launched local proxy).
  This is a prerequisite for the *remote-SaaS* connector story, not for the
  Phase-1 cut (which can use a stdio MCP bridge or the `exec`/`http` backends).
- **Voice / voice-cloning — N/A.** Voice is an I/O modality (ADR 048–050) and
  has no bearing on the tool registry.
- **Tool *discovery* beyond text search + tag/scope filter** (ML-based
  recommendation) — defer, mirroring ADR 041.
- **The `movate`-tier sync protocol + community moderation/CLA** — deferred to
  follow-ups (re-applies ADR 041 D4/D7; schema-ready from day one).
- **No change to the existing per-agent `skills:` mechanism, the four shipped
  backends' contracts, or the `SkillError` taxonomy** — the registry composes
  with them; it does not modify them.

## New surfaces flagged (compat contract — CLAUDE.md rule 5)

All **additive**; none removes or re-types an existing surface:

- The **`exec` / `command` `SkillBackend`** (a new `SkillImplementationKind`
  value + a new sibling backend module).
- The **`ToolDescriptor` format** (`tool.yaml` / the JSON descriptor shape) and
  the `agent.yaml` `tools: [name@version]` block.
- **`/api/v1/tools`** (new endpoints under the ADR-032 contract).
- **`mdk tools`** (`list` / `search` / `add` / `publish`).
- **New storage**: `tool_descriptors` + `tool_descriptor_versions` (behind
  `StorageProvider`, with a migration).
