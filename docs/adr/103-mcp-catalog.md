# ADR 103 — MCP catalog: discover and pin external MCP servers from a curated list (Phase 2)

Status: Accepted
Date: 2026-06-13
Accepted: 2026-06-13 — approved by Jeremy. Ratified: implement the catalog *with* the ADR 104 `MCPRegistrySource` Protocol from the start (bundled + official adapters together) rather than bundled-only-then-refactor; bundled catalog is the default offline source; `mdk mcp add` writes the ADR 101 stanza, never auto-runs.
Deciders: Engineering — a curated catalog + `mdk mcp add` authoring command that
writes the existing ADR 101 `mcp_servers:` stanza (CLAUDE.md §7: extend the
existing seam — declaration + discovery — don't add a parallel one).
Builds on: ADR 101 (the `mcp_servers:` declaration + load-time discovery this
ADR makes *discoverable*; `mdk mcp inspect`), ADR 051 (the built-in connector
pack — `movate/connectors/`, `BUILTIN_CONNECTORS`; the hand-vendored predecessor
this generalizes), ADR 052 (the shared tool registry the catalog complements),
ADR 025 (`mdk mcp serve` — unrelated direction).

## Context

ADR 101 lets an author *declare* an MCP server and have its tools discovered —
**if they already know the server's command/URL.** Today that knowledge is
tribal: an author copies an `npx -y @modelcontextprotocol/server-github`
incantation from a README, guesses the credential env var, and pastes it into
`agent.yaml`. There is no "what MCP servers can I use, and how do I wire one
correctly?" surface.

The predecessor exists but is too rigid: `movate/connectors/` (ADR 051) hand-
vendors a fixed set (ServiceNow, MS Graph, Workday, Salesforce, SAP) as HTTP
skill bundles. It's a curated list, but adding an entry is a code change, and it
predates MCP — every connector is bespoke HTTP, not a reusable MCP server.

Meanwhile the MCP ecosystem now has **public registries** (the official
`registry.modelcontextprotocol.io`, plus vendor catalogs) listing hundreds of
servers with their install command, transport, and auth requirements. The gap is
a movate-side surface that turns "I need GitHub" into a correct, pinned
`mcp_servers:` entry — without the author memorizing package names or auth shapes.

## Decision

A **curated catalog** (data, not code) plus an `mdk mcp` authoring surface that
*writes the ADR 101 stanza* — no new runtime path, no new execution seam. Every
`mdk mcp add` ends in an `mcp_servers:` block that Phase 1 already knows how to
discover and run.

### D1 — A bundled catalog of known MCP servers (curated, versioned data)

A new `src/movate/mcp_catalog/catalog.yaml` (loaded into a typed `MCPCatalog`
model), each entry:

```yaml
- name: github
  title: "GitHub"
  description: "Repos, issues, PRs, code search."
  transport: stdio                # stdio | http
  entry: "npx -y @modelcontextprotocol/server-github@<pinned>"
  credentials: "bearer-from-env:GITHUB_TOKEN"   # the auth shape ADR 101 D3 expects
  homepage: "https://github.com/modelcontextprotocol/servers"
  tags: ["dev", "scm"]
  tools_hint: ["search_repositories", "get_file_contents", ...]   # optional, advisory
```

This is the source of truth in Phase 2 — **bundled, reviewed, pinned**, the same
trust posture as `BUILTIN_CONNECTORS`. It generalizes ADR 051: the connector
pack becomes catalog entries (HTTP connectors stay as-is; new entries are MCP
servers). No network dependency at author time.

### D2 — `mdk mcp add <name>` — resolve from catalog → write `mcp_servers:`

```
mdk mcp add github --agent support-bot          # into agents/support-bot/agent.yaml
mdk mcp add github --project                     # into project.yaml (shared)
mdk mcp add github --tools search_repositories,get_file_contents   # → include_tools
```

Resolves `<name>` from the catalog, runs the existing **`mdk mcp inspect`** probe
to confirm the server is reachable and show its live tools, then writes a
`mcp_servers:` entry (idempotent: updates an existing entry of the same name).
It prints the credential the author must set (`GITHUB_TOKEN`) — it never stores a
secret. Pure authoring: edits YAML, composes the Phase 1 discovery path, no
runtime change.

### D3 — `mdk mcp search <query>` / `mdk mcp list` — browse the catalog

Read-only catalog browsing (name, title, description, transport, tags), so an
author finds a server before adding it. Complements `mdk mcp inspect <entry>`
(which probes a *concrete* server) with catalog-level discovery.

### D4 — Registry source: bundled catalog now; live registry opt-in later

Phase 2 ships the **bundled** catalog only. A live fetch from
`registry.modelcontextprotocol.io` (or a vendor registry) is **opt-in and
deferred** (`mdk mcp search --registry`, gated): pulling an install command from a
remote list at author time is a supply-chain trust decision (a compromised
registry entry suggests a malicious `npx` package) that deserves its own
treatment in the Phase 3 hardening ADR — pinning, digest verification, and an
allowlist of trusted publishers. The bundled catalog needs none of that because
it's reviewed in-repo.

### D5 — Pinning + trust hints

Catalog `entry` values **pin a version** where the ecosystem allows
(`@1.2.3` for npm, an image digest for container servers). `mdk mcp add` warns
when it writes an unpinnable/`latest` entry. The catalog records a `credentials`
hint so `mdk mcp add` can tell the author exactly which secret to provision —
closing the "guessed the wrong env var" failure.

### D6 — Compatibility: purely additive

- New `mcp_catalog/` package + `mdk mcp add`/`search`/`list` subcommands under
  the existing `mcp_app`. No change to `mcp_servers:` schema, the discovery path,
  the runtime, or `BUILTIN_CONNECTORS` (HTTP connectors keep working; they may be
  *represented* in the catalog for discoverability without changing execution).
- `mdk mcp add` only writes YAML an author could write by hand — the output is
  exactly the ADR 101 stanza.

## Boundary (out of scope)

- **Remote-server trust hardening** (live-registry fetch verification, OAuth
  servers, digest pinning enforcement, rate limiting) — Phase 3, its own ADR.
- **Persisting discovered tools into the durable tool registry (ADR 052)** —
  still Phase 3; the catalog seeds *declarations*, not registry rows.
- **Auto-installing servers** (running `npm i` / pulling images) — `mdk mcp add`
  writes the declaration; provisioning the runtime (the `INSTALL_NODE` image,
  ADR 101) stays a deploy concern.

## Alternatives considered

- **Extend `BUILTIN_CONNECTORS` in code instead of a data catalog.** Rejected:
  adding a server should be a data/PR change reviewable by non-Python folks, and
  the catalog must scale to many entries without bloating an importable dict.
- **Live registry as the primary source.** Rejected for Phase 2: author-time
  network dependency + supply-chain trust (see D4). Bundled-first, live-opt-in
  later is the safer sequence.
- **A separate `connectors:` stanza.** Rejected: it would fork declaration from
  the ADR 101 `mcp_servers:` path. `mdk mcp add` writing `mcp_servers:` keeps one
  declaration + one discovery path.

## Consequences

- "I need GitHub" becomes `mdk mcp add github` — resolved, probed, pinned, with
  the right credential surfaced — instead of copy-pasting an incantation.
- The ADR 051 connector pack generalizes into a catalog that scales by data.
- One declaration surface (`mcp_servers:`) and one discovery path (ADR 101) serve
  both hand-authored and catalog-added servers.
- Estimated scope: ~2 PRs — (1) `mcp_catalog/` model + bundled `catalog.yaml` +
  `mdk mcp list`/`search`; (2) `mdk mcp add` (resolve → inspect → write stanza,
  idempotent). Live-registry fetch is a later, gated addition.
