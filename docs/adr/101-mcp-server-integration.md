# ADR 101 — MCP integration: declare external MCP servers once, discover their tools through the governed registry

Status: Accepted
Date: 2026-06-13
Accepted: 2026-06-13 — approved by Jeremy. Ratified: agent+project declaration scope; default fail-soft discovery (`required: false`); in-memory per-load descriptor mints (durable-registry persistence deferred to Phase 3); no official `mcp` SDK (reuse the hand-rolled client).
Deciders: Engineering — additive `mcp_servers:` stanza on the existing
`agent.yaml` / `project.yaml` surfaces and a load-time discovery phase that mints
tools through the existing tool registry (CLAUDE.md §7: extend the existing
seams — `SkillBackend`, the tool registry — don't add a second tool-execution
path).
Builds on: ADR 002 (skills + the `SkillBackend` Protocol / `dispatch_skill`; the
`kind: mcp` backend this ADR reuses verbatim), ADR 052 (the shared tool registry
— `ToolDescriptor`, scope precedence, `default_grant`/allowlist governance,
`credentials_ref` — the catalog discovered tools land in), ADR 051 (connector
skill stubs — the hand-vendored predecessors of registry-discovered tools),
ADR 025 (`mdk mcp serve` — movate *as* an MCP server; the **other** direction,
not extended here), ADR 093 (governance gates; the SKILL gate that already sees
declared skills), ADR 097 (the `tool` node — the additive-behind-an-existing-seam
playbook this ADR follows).

## Context

Movate already speaks MCP — but only one server, one skill, one `agent.yaml`
entry at a time.

Exploration finding: **the execution machinery is complete and already supports
the whole-server case.** The gap is *declaration and discovery*, not transport.

- **MCP client (consume external servers).** `MCPSkillBackend`
  (`core/skill_backend/mcp.py:170`, ~1,160 lines) dispatches `kind: mcp` skills
  over stdio (subprocess, newline-delimited JSON-RPC) or HTTP/SSE, with
  connection pooling, SkillError mapping, and an `mcp.call` child span. It
  already supports **two tool modes** (`mcp.py:22-24`): single-tool (the skill
  names one `tool:`) and **multi-tool** (`tool:` omitted → the backend calls
  `tools/list` at `mcp.py:463` and registers every tool as a namespaced callable
  `<skill-name>.<tool-name>`, which the executor sees as multiple tools). The
  backend auto-registers on import (`mcp.py:1159`).
- **A governed catalog already exists.** The shared tool registry (ADR 052)
  carries `ToolDescriptor` (`core/tool_registry/models.py:94`) with a
  `kind: "mcp"|"exec"|"http"` backend (`ToolBackendConfig`,
  `models.py:40`), a `scope` (`models.py:116`), `governance.default_grant`
  (`models.py:85`), and a `credentials_ref` (`models.py:140`). `ToolResolver`
  (`resolver.py:127`) walks scope precedence project→tenant→movate
  (`resolver.py:164`) and enforces the per-agent allowlist when
  `default_grant=false` (`resolver.py:188`). `tool_descriptor_to_skill_bundle`
  (`tool_registry/bridge.py`) already maps `"mcp"` →
  `SkillImplementationKind.MCP` (`bridge.py:35,61`).
- **Movate *as* an MCP server** ships too — `mdk mcp serve` (`cli/mcp_cmd.py`,
  ADR 025) exposes the authoring catalog as MCP tools. That is the inverse
  direction and is **out of scope** here.
- **Dependency posture is settled.** Both the skill backend and the authoring
  server hand-roll JSON-RPC over the stdlib rather than pull the heavy official
  `mcp` SDK (`cli/mcp_cmd.py:18-22`, CLAUDE.md §8). This ADR keeps that decision.

So the gap is **two residual surface gaps** that keep MCP from being a
first-class integration surface:

1. **No server-level declaration.** To give an agent an external MCP server's
   tools today, an author writes a `skill.yaml` under `skills/<name>/` with
   `kind: mcp` and `entry: <command|url>`, then lists that skill in the agent's
   `skills:` (`core/models.py:1578`). One server = one hand-authored skill file.
   There is no place to say "this agent (or every agent in this project) may use
   the GitHub MCP server" once. Multi-tool mode gives you the *tools* but still
   needs the per-server skill file and gives the registry/governance layer
   nothing to see — the tools never become `ToolDescriptor`s.
2. **Discovery is manual.** The connector predecessors (`connectors/` — SAP,
   Salesforce, Workday, MS Graph; ADR 051) are hand-vendored `skill.yaml`
   stubs. There is no load-time enumeration of a server's actual `tools/list`
   into the governed catalog, so governance (allowlist, `credentials_ref`,
   tenant scope) and observability of a server's tools only exist for tools
   someone manually transcribed.

This ADR closes gap 1 and the load-time half of gap 2. A *browsable* catalog
sourced from a public MCP registry (gap 2's discovery-UX half) and remote-server
trust hardening are deferred to follow-up ADRs (see Boundary).

## Decision

One additive stanza, two declaration scopes, **zero new execution paths**: every
decision below is an additive field or a load-time phase on the existing
`agent.yaml`/`project.yaml` + tool-registry machinery, and every discovered tool
is dispatched by the **unchanged** `MCPSkillBackend` through the **unchanged**
`dispatch_skill` the executor already calls (`executor.py:667`).

### D1 — `mcp_servers:` stanza on `agent.yaml` and `project.yaml`

A new optional `mcp_servers: list[MCPServerRef]` block, declarable at two scopes
that compose with the existing layered-defaults model (agent wins; project fills):

- **`agent.yaml`** — servers this agent may use. New field on `AgentSpec`
  (`core/models.py:1426`), default `[]` (relaxes nothing — `extra="forbid"` at
  `models.py:1429` stays; the field is simply declared).
- **`project.yaml`** — servers shared by every agent in the project. New field on
  the project config model (`core/config.py`, `extra="forbid"` at
  `config.py:645`). Unlike the `defaults:` block (which merges only
  `model.params`/timeouts/budget via `layered_defaults`), `mcp_servers` is a
  **union by server `name`**: an agent's list is appended to the project's, and
  an agent entry with the same `name` **overrides** the project entry wholesale
  (the agent author's explicit intent wins). Empty/absent at both scopes =
  today's behavior, byte-for-byte.

`MCPServerRef` (new model, `extra="forbid"`):

```yaml
mcp_servers:
  - name: github                       # registry/skill namespace for its tools
    entry: "npx -y @modelcontextprotocol/server-github"   # stdio command…
    # entry: "https://mcp.internal/github"                # …or HTTP(S) URL
    include_tools: ["search_repositories", "get_file_contents"]  # optional allowlist
    # exclude_tools: ["delete_*"]      # optional denylist (mutually exclusive with include)
    credentials_ref: "kv://github-mcp-token"   # optional; injected per D3
    required: false                    # optional; load-time failure policy (D5)
```

- `name` — the namespace under which the server's tools are registered
  (`<name>.<tool>`, reusing the existing multi-tool convention at
  `mcp.py:22-24`). Must be unique within the merged list.
- `entry` — a stdio command (tokenized with `shlex.split`, as `mcp.py:385`
  already does) or an `http(s)://` URL. Same field semantics as
  `SkillImplementation.entry` for `kind: mcp` (`models.py:519`).
- `include_tools` / `exclude_tools` — optional, mutually exclusive. The
  discovery filter (D2). Absent → every tool the server reports.
- `credentials_ref` — optional pointer resolved at dispatch (D3); mirrors
  `ToolDescriptor.credentials_ref` (`models.py:140`).
- `required` — load-time failure policy (D5); default `false`.

### D2 — Load-time discovery: enumerate `tools/list`, mint `ToolDescriptor`s, register as skills

A new discovery phase runs inside `load_agent` (`core/loader.py:290`) **after**
spec parse and **before** `resolve_agent_skills` (`core/skill_loader.py:249`),
in a new module `core/mcp_discovery.py` (parallel to `skill_loader.py`; keeps
`loader.py` an orchestrator). For each merged `MCPServerRef`:

1. **Connect + list.** Open the server via the existing `MCPSkillBackend`
   transport (`mcp.py` stdio/HTTP), call `tools/list` (`mcp.py:463`). Reuse the
   backend's connection pooling — no second client implementation.
2. **Filter.** Apply `include_tools`/`exclude_tools` to the reported tool set.
3. **Mint descriptors.** For each surviving tool, construct an in-memory
   `ToolDescriptor` (`tool_registry/models.py:94`): `name = "<server>.<tool>"`,
   `scope = project|movate` (agent-declared → ephemeral/project; project-declared
   → project), `backend = ToolBackendConfig(kind="mcp", entry=..., tool=<tool>)`,
   `credentials_ref` from the ref, input/output schema from the tool's reported
   JSON Schema, `governance.default_grant` derived from the filter (D3).
4. **Resolve to skills.** Feed the descriptors through the existing
   `tool_descriptor_to_skill_bundle` (`tool_registry/bridge.py:61`) so each
   becomes a `SkillBundle` (`skill_loader.py:44`) entry indistinguishable from a
   `skills/`-sourced or registry-sourced skill. Append to the agent's resolved
   skill list.

The executor is **untouched**: it builds `tool_specs` via `to_tool_spec`
(`executor.py:667`) over the resolved bundle and dispatches each call through
`dispatch_skill`, which routes `SkillImplementationKind.MCP` to the same
`MCPSkillBackend` instance. A discovered tool and a hand-written `kind: mcp`
skill are the same thing downstream.

Discovery results are cached on the loaded agent (keyed by the agent's existing
content hash + a server fingerprint, D5) so repeated loads in one process don't
re-handshake every server.

### D3 — Governance: discovered tools obey the existing allowlist + credential seams

Discovery does not invent a new trust model — it populates the ADR 052 one:

- **Allowlist via the filter.** When `include_tools` is set, only those tools are
  minted (`default_grant=true` for the minted set — the author already named
  them). When neither filter is set, *all* reported tools are minted with
  `default_grant=true` (the author opted into the whole server by declaring it).
  When `exclude_tools` is set, the complement is minted. There is no path by
  which a tool the author didn't (directly or by omission) authorize reaches the
  agent — the same guarantee `ToolResolver`'s allowlist check gives
  (`resolver.py:188`).
- **Credentials.** `credentials_ref` resolves at **dispatch** time, not load
  time, through the same credential-resolution seam the registry's
  `credentials_ref` already uses — injected into the stdio child's env (stdio)
  or as an `Authorization` header (HTTP) by the backend. A missing/denied
  credential is a `SkillError(BACKEND_ERROR)` at call time, never a crash.
- **Governance gate visibility.** Because discovered tools become declared
  `ToolDescriptor`s/`SkillBundle`s, the ADR 093 SKILL gate and `mdk validate`
  see them — closing the "wrapper hides a side-effect" blind spot ADR 097 called
  out, now for whole MCP servers.
- **Tenant scope.** Project/movate-scoped descriptors carry the tenant the
  project resolves to; an HTTP server URL is never cross-tenant by construction
  (the ref lives in that project's `project.yaml`).

### D4 — `mdk` surface: inspect what a server exposes before wiring it

One read-only addition shipped, one deferred (no new execution path):

- **`mdk mcp inspect <entry>`** (new subcommand under the existing `mcp_app`,
  `cli/mcp_cmd.py`): connect to a server (stdio command or URL), print its
  `tools/list` alongside the namespaced skill identifier discovery would mint,
  **without** writing anything. The "what would I get if I declared this?"
  probe. Composes only the existing backend's `discover_tools` list path; no
  filesystem writes. Auth uses ambient env (credential-ref injection is a
  run-time concern, D3). **Shipped.**
- **`mdk agent tools <agent>` MCP section** — listing realized
  `<server>.<tool>` skills per agent. **Deferred:** there is no `mdk agent
  tools` surface today (`mdk tools` is the ADR 052 *registry*, not an agent's
  resolved skills), so this is its own small follow-up rather than part of this
  slice. Discovered tools already flow into the executor regardless.

`mdk mcp serve` (ADR 025, movate-as-server) is unchanged and unrelated.

### D5 — Failure modes: a flaky server must not brick agent load

MCP servers are external processes/endpoints; discovery is a network/subprocess
boundary and is treated as one (CLAUDE.md §10):

- **Unreachable / handshake failure / `tools/list` timeout.** Governed by
  `required` (D1). `required: false` (default) → **fail-soft**: the agent loads
  *without* that server's tools, and a structured warning is emitted (and
  surfaced by `mdk validate` / `mdk agent tools`). `required: true` → **fail-hard**:
  `load_agent` raises, so a server the agent genuinely depends on can't silently
  vanish. Default-soft because the common case (an optional enrichment server
  down) shouldn't take an agent offline.
- **Per-server timeout.** Discovery uses a bounded `tools/list` timeout
  (reusing the backend's existing timeout config), so one hung server can't hang
  load; a timeout is treated as unreachable per `required`.
- **Tool drift.** The minted descriptor set is fingerprinted (sorted tool names
  + schema hash) and recorded with the load. `mdk validate` re-lists and warns
  when the live server's tools no longer match what an author last saw — the
  "resolve-time vs now" drift the certification suite cares about. Drift is a
  warning, not a hard failure (the server is authoritative at dispatch).
- **Name collision.** A `<server>.<tool>` that collides with an existing
  `skills:` name or another server's namespaced tool is a load-time error with
  the offending names — deterministic, not last-writer-wins.

### D6 — Compatibility: purely additive

- `AgentSpec` (`models.py:1426`) and the project config model (`config.py:645`)
  keep `extra="forbid"`; `mcp_servers` is a newly *declared* optional field
  (default `[]`), so existing `agent.yaml`/`project.yaml` files load unchanged
  and a typo'd key still fails loudly.
- The `SkillBackend` Protocol, `MCPSkillBackend`, `dispatch_skill`, the executor
  tool-use loop, and the `ToolDescriptor`/`ToolResolver` contracts are
  **untouched** — discovery composes them, it does not modify them.
- Existing hand-written `kind: mcp` skills under `skills/` keep working exactly
  as before; an author can mix declared servers and hand-written MCP skills.
- The shared tool registry (ADR 052) is unaffected: discovered descriptors are
  *in-memory* per-load mints, not writes to the persisted registry. (Persisting
  discovered tools into the registry is a Phase-2 option, not done here.)
- No `/api/v1`, `--json`, env-var, storage-schema, or deploy change. The one
  flagged surface change (CLAUDE.md §5) is the additive `mcp_servers` field on
  the `agent.yaml` and `project.yaml` schemas — additive and default-empty.
- **No new shipped dependency.** Discovery reuses the hand-rolled JSON-RPC client
  already in `mcp.py`; the official `mcp` SDK is still not pulled in.

## Boundary (out of scope)

- **Browsable connector catalog from a public MCP registry** — `mdk mcp add
  <name>` seeded from an upstream registry, turning hand-vendored `connectors/`
  (ADR 051) into a discoverable, pinnable list. The discovery-UX half of the
  Context's gap 2. **Phase 2, its own ADR.**
- **Remote-server trust hardening** — server identity/health checks, signed
  manifests, rate limiting, per-call audit beyond the existing `mcp.call` span,
  and persisting discovered descriptors into the durable registry. **Phase 3,
  its own ADR.**
- **Movate as an MCP server** (`mdk mcp serve`, ADR 025) — the inverse
  direction; unchanged.
- **OAuth / interactive-auth MCP servers** — the credential seam here is
  ref-based (env/header injection). Servers requiring an interactive OAuth dance
  are deferred to the Phase 3 trust work.
- **The tool-use turn cap** — discovered tools count against the existing
  per-agent tool-turn cap unchanged; raising it is not part of this ADR.

## Alternatives considered

- **Status quo: one `skill.yaml` per server (multi-tool mode).** Already works
  — multi-tool mode (`mcp.py:22-24`) gives an agent a whole server's tools from
  one skill file. Rejected as the *product* answer: it needs a hand-authored
  file per server, gives the registry/governance/observability layer nothing to
  see (the tools never become `ToolDescriptor`s), and has no project-level
  "share this server across agents" scope. D1+D2 keep that backend and add the
  declaration + discovery + governance the file approach can't.
- **Registry-only (no YAML stanza).** Register each MCP server's tools into the
  durable tool registry (ADR 052) out-of-band and reference them by name in
  `skills:`. Most governed, but worst authoring ergonomics — every demo/customer
  agent needs a registry-population step before it can use a public server, and
  there's no "just point at this server" path. Rejected as the *default*; the
  registry remains available for teams that want curated, persisted tools, and
  Phase 3 can persist discovered descriptors into it.
- **Pull the official `mcp` SDK.** Would replace the hand-rolled client. Rejected
  for the same reason ADR 025 and the skill backend rejected it: the SDK is heavy
  for what we use (stdio + HTTP `tools/list`/`tools/call`), and CLAUDE.md §8
  favors the composable stdlib client we already maintain and trace. Revisit only
  if a transport we need (e.g. the SDK's auth flows) isn't worth re-implementing
  — likely surfacing in the Phase 3 OAuth work.
- **Per-call discovery instead of load-time.** Call `tools/list` lazily on first
  tool use rather than at load. Rejected: governance and `mdk validate` need the
  toolset *at author/deploy time*, not first-request time; load-time discovery
  (cached, D2) makes the realized toolset inspectable before an agent ever runs.
  The backend still lists lazily for hand-written multi-tool skills — that path
  is unchanged.
- **Discovery writes to the durable registry on load.** Persist minted
  descriptors so the registry is the source of truth. Rejected for Phase 1:
  load-time writes couple agent loading to registry storage availability and
  raise concurrency/staleness questions (two agents discovering the same server).
  In-memory mints keep load pure; persistence is an explicit Phase-3 decision.

## Consequences

- An author declares an MCP server once — per agent or per project — and its
  tools appear, governed and observable, with no per-server `skill.yaml`. The
  GitHub/Slack/Jira-MCP-server story becomes three lines in `agent.yaml`.
- Discovered tools are first-class `ToolDescriptor`s/`SkillBundle`s:
  `mdk validate`, the ADR 093 SKILL gate, the allowlist/`credentials_ref` seams,
  and the per-call `mcp.call` span all see them — closing the wrapper-hides-a-
  side-effect blind spot for whole servers.
- A flaky/optional server degrades gracefully (`required: false` default); a
  load-bearing one fails loud (`required: true`). Drift is surfaced, not silent.
- The hand-rolled JSON-RPC client stays the one MCP client; no new dependency,
  no second tool-execution path.
- Risks accepted: load-time discovery adds a bounded network/subprocess cost to
  `load_agent` for agents that declare servers (mitigated by caching + per-server
  timeout + default fail-soft); in-memory descriptors mean discovered tools
  aren't queryable in the durable registry until Phase 3 (acceptable — the
  declaration *is* the source of truth in Phase 1).
- Estimated scope: ~3 PRs — (1) `MCPServerRef` model + `mcp_servers` on
  `AgentSpec`/project config + merge semantics (model + layered merge); (2)
  `core/mcp_discovery.py` + `load_agent` wiring + descriptor mint via the
  existing bridge + failure/drift policy; (3) `mdk mcp inspect` + `mdk agent
  tools` MCP section. Each independently additive and default-off.
