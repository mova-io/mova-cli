# ADR 104 — External MCP registry sources: pluggable inbound discovery behind one Protocol

Status: Accepted
Date: 2026-06-13
Accepted: 2026-06-13 — approved by Jeremy. Ratified: one `MCPRegistrySource` Protocol, not four hardcoded paths; bundled+official ON by default, community (mcp.so/Glama) opt-in + pin/warn; inbound import only — outbound publish deliberately deferred to its own ADR + product sign-off.
Deciders: Engineering — a `MCPRegistrySource` adapter Protocol with one
implementation per external registry (CLAUDE.md §7: new source = new adapter
behind an existing seam, not four hardcoded integrations). Inbound (import) only;
outbound publish is an explicit boundary (see below).
Builds on: ADR 103 (the curated catalog model + `mdk mcp add`/`search`/`list`
this generalizes — an external registry becomes another *source* feeding the same
commands), ADR 101 (the `mcp_servers:` declaration + load-time discovery every
import ultimately writes), ADR 051 (the built-in connector pack — the bundled
source), ADR 025 (`mdk mcp serve` — the outbound MCP surface this ADR does NOT
extend), ADR 052 (the tool registry, complemented not replaced).

## Context

ADR 103 ships a **bundled, curated** MCP catalog and deferred any *live* registry
fetch, flagging it a supply-chain decision. The ecosystem now has several public
registries an author would reasonably want MDK to read:

| Registry | Character | Why integrate (inbound) |
|---|---|---|
| **Official MCP Registry** (`registry.modelcontextprotocol.io`) | Canonical; HTTP API + `server.json` schema; designed as a *metaregistry* others mirror. | The default live source — one-import of any well-known server. |
| **GitHub MCP Registry** | GitHub-hosted, large developer audience, discoverable/installable packages. | Sync metadata + import GitHub-published servers. |
| **mcp.so** | Popular community directory, broad discovery. | Browse/import a wider community set. |
| **Glama MCP Directory** | Extensive community catalog of public servers/tools. | Browse/import an extensive community set. |

The wrong way to absorb four registries is four bespoke code paths. The right way
is a **single source Protocol** the ADR 103 commands already consume — the
bundled catalog becomes "just another source," and each external registry is an
adapter. This ADR adds the *seam* + the first adapters and, crucially, the
**trust model** that separates the canonical registry from community directories.

This ADR is **inbound only**. The table's outbound items — *publishing MDK MCP
servers* and *making MDK agents exportable/searchable* on these directories — are
a product + security + naming-ownership decision (who owns the listing? what is
the published artifact — an agent-as-tool? how is it versioned and authed?), with
`mdk mcp serve` (ADR 025) as the relevant prior art. They are an explicit
boundary here and need their own ADR + product sign-off (see Boundary).

## Decision

One Protocol, many sources, **zero new runtime/execution path**: every import
ends in an ADR 101 `mcp_servers:` entry that Phase 1 already discovers and runs.

### D1 — `MCPRegistrySource` Protocol (the adapter seam)

A read-only source contract (`src/movate/mcp_catalog/sources/base.py`):

```python
class MCPRegistrySource(Protocol):
    name: str          # "bundled" | "official" | "github" | "mcp.so" | "glama"
    trust: TrustTier   # CURATED | OFFICIAL | COMMUNITY

    async def search(self, query: str, *, limit: int = 25) -> list[CatalogEntry]: ...
    async def get(self, ref: str) -> CatalogEntry | None: ...
```

`CatalogEntry` is the ADR 103 catalog model (name/title/transport/entry/
credentials-hint/tags) plus provenance (`source`, `trust`, `pinned`,
`publisher`). The bundled catalog (ADR 103) is reframed as the
`BundledSource` — the `CURATED` tier, always present, the default.

ADR 103's `mdk mcp search`/`list`/`add` gain a `--source` selector (default:
`bundled`, then `official`); they call the resolved source's `search`/`get` and
otherwise behave identically — `add` still resolves → `mdk mcp inspect` →
writes the `mcp_servers:` stanza (idempotent, pinned).

### D2 — Official MCP Registry adapter (the default live source)

`OfficialRegistrySource` (`trust: OFFICIAL`) reads the canonical registry's HTTP
API and maps `server.json` → `CatalogEntry` (package/transport → `entry`,
environment-variable requirements → the `bearer-from-env:` credential hint,
namespace → `publisher`). Because the official registry is a metaregistry, this
single adapter already surfaces much of what the others list. It is the default
live source behind the bundled catalog.

### D3 — GitHub MCP Registry adapter

`GitHubRegistrySource` (`trust: OFFICIAL` — GitHub-curated) maps GitHub's MCP
registry metadata → `CatalogEntry`. Same import path; entries pin a package
version / ref where the source provides one.

### D4 — Community directories (mcp.so, Glama) behind an explicit trust gate

`McpSoSource` / `GlamaSource` (`trust: COMMUNITY`). These broaden discovery but
list arbitrary community servers, so:

- **Never default.** A `COMMUNITY` source is only consulted with an explicit
  `--source mcp.so` / `--source glama` (or `--source all`, which still tags each
  result with its tier).
- **Loud provenance.** `mdk mcp search`/`add` render the `trust` tier; adding a
  `COMMUNITY` entry prints a one-line warning naming the source and publisher.
- **Pin or warn.** `add` pins a version/digest when the entry provides one and
  **warns** when it can only write an unpinned/`latest` command (an unpinned
  `npx` from a community directory is the live-RCE-at-deploy risk).
- **No auto-install / no auto-run.** Import writes a *declaration*; provisioning
  the runtime (the `INSTALL_NODE` image, ADR 101) and actually spawning the
  server stay separate, later, deliberate steps.

### D5 — Trust model (the crux)

Importing from a registry must not silently become "run this stranger's code."
The invariants:

1. **Import ≠ execute.** `mdk mcp add` only writes YAML the author could type by
   hand. Nothing runs until an agent loads (ADR 101 discovery) on a runtime an
   operator provisioned.
2. **Tiered trust.** `CURATED` (bundled, in-repo reviewed) > `OFFICIAL`
   (canonical / GitHub registries) > `COMMUNITY` (mcp.so / Glama, opt-in only).
   The tier travels with every entry and is shown to the author.
3. **Pinning is the default intent.** Prefer version/digest-pinned `entry`
   values; warn on every unpinnable write regardless of source.
4. **Offline-first.** The bundled `CURATED` source needs no network; live
   sources are additive. A registry being down degrades to "fewer results," never
   a failed author session.
5. **No secrets in transit to registries.** Sources are read-only metadata
   fetches; credentials are resolved at use time from env (ADR 101 D3), never
   sent to a registry.

### D6 — Caching + resilience

Live-source results are cached locally (short TTL) so repeated `search` doesn't
hammer a registry, and a source timeout/error is fail-soft per source (the others
+ bundled still return). One slow community directory can't stall discovery.

### D7 — Compatibility: purely additive

- New `mcp_catalog/sources/` package + `--source` flags on the ADR 103 commands.
  No change to `mcp_servers:` schema, the discovery path, the runtime, the tool
  registry, or `mdk mcp serve`.
- The bundled catalog stays the default; teams that never pass `--source` see
  only `CURATED` entries — identical to ADR 103.
- New shipped dependency: none required beyond the existing `httpx`; any
  source-specific SDK would be an opt-in extra, justified per CLAUDE.md §8.

## Boundary (out of scope — explicit)

- **Outbound publish (MDK → registries).** "Publish MDK MCP servers" and "make
  MDK agents exportable/searchable" on these directories is a separate decision:
  it defines a *published artifact* (an MDK agent/skill exposed via `mdk mcp
  serve`, ADR 025), ownership of the registry listing, naming/namespacing,
  versioning, and the security of an externally-discoverable MDK surface. It
  needs its own ADR and product sign-off — deliberately not bundled with inbound
  import. The `MCPRegistrySource` Protocol is read-only; a future
  `MCPRegistryPublisher` seam would be its outbound counterpart.
- **Live-source supply-chain hardening beyond pin+tier+warn** (signature/digest
  *verification*, a trusted-publisher allowlist, provenance attestation) — the
  Phase 3 hardening ADR. This ADR establishes the tiers + pinning; enforcement
  teeth come there.
- **Auto-provisioning servers** (running `npm i` / pulling images on import) —
  out of scope; declaration only.

## Alternatives considered

- **Hardcode the four registries directly in the CLI.** Rejected: four code
  paths, no shared trust model, and every new registry is a CLI change. The
  Protocol makes a source an adapter (and a test a fake).
- **Treat all sources as equal.** Rejected: it erases the canonical-vs-community
  trust distinction that is the entire safety story. Tiering is load-bearing.
- **Default to the union of all sources.** Rejected: it would surface unvetted
  community `npx` commands by default. Bundled+official by default; community
  opt-in.
- **Bundle inbound + outbound in one ADR (the table as written).** Rejected:
  outbound is a product/security decision with different stakeholders and a
  different seam (`mdk mcp serve`). Coupling them would stall the safe, ready
  inbound work behind the harder publish question.
- **Skip the official registry, integrate community dirs first** (they have more
  entries today). Rejected: trust posture. Canonical first; breadth via gated
  community sources.

## Consequences

- `mdk mcp search github --source official` → `mdk mcp add github` imports a
  pinned, credential-hinted `mcp_servers:` entry from the canonical registry —
  the ADR 103 experience, now ecosystem-wide.
- Adding a registry later is an adapter + a row in the source registry, not a CLI
  rewrite; community sources are reachable but never silently trusted.
- The author always sees where an entry came from and whether it's pinned; the
  runtime path (ADR 101) is unchanged and unaware of where a declaration
  originated.
- Outbound publish remains an open, separately-owned decision — this ADR neither
  commits to nor precludes it, and names the seam it would use.
- Estimated scope: ~3 PRs — (1) `MCPRegistrySource` Protocol + `BundledSource`
  refactor + `--source` plumbing; (2) `OfficialRegistrySource` (+ caching,
  fail-soft); (3) GitHub adapter, then community adapters behind the trust gate.
  Each additive and default-off for anything beyond bundled+official.
