# ADR 041 — Agent catalog: Movate-curated, tenant-private, community-ready

**Status:** Proposed
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x productization — grow the bundled-template gallery
into a cloud-hosted catalog of reusable agents that customers can browse, pull,
customize, and (later) contribute back.
**Builds on / related:** ADR 014 (durable agent registry — the destination
when a catalog entry is added to a project), ADR 028 (template discoverability
+ use-case metadata — the shape taxonomy the catalog inherits), ADR 032
(front-end `/api/v1` surface — the catalog endpoints extend it), ADR 039
(Movate-side hosted services — the operating posture for `catalog.movate.io`),
ADR 040 (projects — the consumer that adds catalog entries into a tenant
registry), `src/movate/templates/` (today's bundled gallery), the
`StorageProvider` Protocol (`src/movate/storage/base.py`).

## Context

Today MDK ships a small set of agent templates bundled into the image
(`mdk add --list` / `mdk templates`, surfaced by ADR 028 / PR #521). The
gallery is rich (~16 shapes — `faq`, `rag_qa`, `classifier`, `extractor`,
`summarizer`, `ticket_triager`, `sql_writer`, `lookup`, `research`,
`code_reviewer`, `compliance_checker`, `email_responder`, `lead_qualifier`,
`meeting_summarizer`, `resume_screener`, `workflow_init`, …) but the gallery
is **frozen at image-build time**: customers cannot pull a new starter without
upgrading MDK, cannot publish their own internal templates for reuse across
their org, and have no path to a wider community catalog.

Deva wants to evolve this into a **cloud-hosted catalog of reusable agents**
that backs Mova iO's planned "Agent Marketplace" surface. Three audiences,
three namespaces:

1. **Movate-curated.** The official catalog. Source of truth lives in a
   GitHub repo `movate/agent-catalog`; CI validates entries (schema + lint +
   smoke-eval against `--mock`) and publishes to `catalog.movate.io` on
   merge to `main`.
2. **Tenant-private.** Each customer's own reusable agents, scoped to their
   tenant. Submitted via `/api/v1/catalog/agents`, stored in the customer's
   own Postgres, **never synced upward**.
3. **Community.** Future — user-contributed, moderated. Schema-ready from
   day one; moderation + CLA process is a separate ADR.

This ADR records the catalog's **architecture** (hosting, schema, sync
protocol, API surface, namespace model, data sovereignty) — not the rollout
plan, the UI, or the moderation policy.

## Decision

A **single catalog schema** spans all three namespaces and is read through a
**single `/api/v1/catalog` surface**, populated by a **hybrid hosting model**
(image bundle as the floor, `catalog.movate.io` as the live source of truth,
customer Postgres as the cache). "Adding from catalog" produces a **NEW
agent in the project's registry**, decoupled from the catalog entry it was
cloned from.

### D1 — Hybrid hosting (image bundle floor + cloud sync + Postgres cache)
The image ships a **floor of N curated templates** (today's gallery, ADR 028)
so `mdk init` works without network. The runtime additionally **syncs from
`catalog.movate.io`** on first deploy and periodically (ADR 017's scheduler),
caching the result in customer Postgres (the `catalog_entries` table —
**D2**). On read, the API queries the local cache; air-gapped customers stay
on the bundle floor plus `mdk catalog import-bundle` (**Open question 5**).

### D2 — Three namespaces, one schema (`source` column)
All catalog entries — Movate-curated, tenant-private, and (future) community
— live in **one** `catalog_entries` table, distinguished by a `source` column:
`"movate"` | `"private"` | `"community"`. `tenant_id` is **NULL for public**
entries (`movate`, `community`) and **set for private**. Uniqueness:
`(slug, source, tenant_id)` covers all three namespaces in a single table.
Reads always filter by "`source = 'movate'` OR (`source = 'private'` AND
`tenant_id = $caller`)". The community case is column-ready but no rows are
written until the moderation ADR lands (**D7**).

### D3 — Movate-curated source of truth = GitHub repo + CI
The canonical Movate catalog lives in a GitHub repo
`movate/agent-catalog`, one directory per entry (`agent.yaml`, `prompt.md`,
schemas, optional dataset, `catalog.yaml` metadata: tags, shape,
`recommended_for`, semver). PRs run CI:
1. JSON-schema validation of the entry manifest;
2. `ruff` / `mypy` lint on any included code;
3. **smoke-eval** against `mdk eval --mock` (no live providers) on the seed
   dataset.
On merge to `main`, CI publishes the entry bundle (tar) + manifest to
`catalog.movate.io` (writes a row in the Movate-side Postgres). This makes
the Movate-curated catalog a **reviewed-PR-only** artifact — the same
governance bar as any shipped MDK template.

### D4 — Sync protocol (watermark-incremental)
Customer runtime → `GET catalog.movate.io/v1/catalog/agents?since=<watermark>`
returns deltas (entries added, version-bumped, deprecated) since the
caller's last sync. The runtime **upserts** into local `catalog_entries` +
`catalog_entry_versions` and advances its watermark. Schedule via ADR 017's
job scheduler (default: daily). `POST /api/v1/catalog/sync` (admin scope)
triggers a manual sync. Bundle tars (the actual `agent.yaml` + files for a
given version) are fetched lazily on **add** — the sync only pulls
manifests + digests (~50 MB cached per typical sync).

### D5 — Tenant-private = customer Postgres only (sovereignty)
Tenant-private entries (`source = "private"`, `tenant_id` set) live **only**
in the customer's own Postgres and are **never** synced upward to
`catalog.movate.io`. Submission flows through `POST /api/v1/catalog/agents`
on the customer's own runtime, which writes to the same `catalog_entries`
table with `tenant_id` set and `source = "private"`. The customer's data
plane is the only authority for that namespace.

### D6 — Adding from catalog = clone-and-decouple
`POST /api/v1/projects/{id}/agents` (ADR 040) gains a new source:
```jsonc
{ "source": "catalog",
  "slug": "ticket_triager",
  "version": "1.4.0",
  "rename_to": "support_triager",   // optional
  "overrides":  { "model": "azure/gpt-4o" }  // optional
}
```
This **clones** the catalog bundle into the project's durable registry
(ADR 014). The result is a **NEW agent**, owned by the project, decoupled
from the catalog source: catalog upgrades **do not** re-sync to consumers.
Re-pulling a newer version means an explicit second `POST` with the
new `version` (and a `rename_to` if the caller wants to keep both).
Rationale: agents are customized after add (prompt edits, BYO eval data,
overrides) — automatic re-sync would silently overwrite customer work.

### D7 — Community namespace: deferred, schema-ready
The `source` column accepts `"community"` from day one and the API treats
it the same as `"movate"` for **reads** (public, no tenant filter). **No
writes** are accepted into `source = "community"` until a separate ADR
defines:
- The CLA / contributor agreement;
- Moderation workflow (who can merge to a public namespace?);
- Trust signals (publisher identity, signed bundles?);
- Reputation / takedown.
Deferring this avoids designing a marketplace before a curated catalog has
proven the schema.

## Schema (DDL-ish)

```sql
CREATE TABLE catalog_entries (
  slug              TEXT NOT NULL,                       -- e.g. "ticket_triager"
  source            TEXT NOT NULL CHECK (source IN ('movate','private','community')),
  tenant_id         TEXT,                                -- NULL for movate/community
  latest_version    TEXT NOT NULL,                       -- semver (D-Open-Q 2)
  name              TEXT NOT NULL,
  title             TEXT NOT NULL,
  description       TEXT NOT NULL,
  tags              TEXT[] NOT NULL DEFAULT '{}',
  shape             TEXT,                                -- ADR 028 taxonomy
  recommended_for   TEXT,                                -- one-line use-case
  ratings_summary   JSONB NOT NULL DEFAULT '{}',         -- {count, avg}
  popularity        INTEGER NOT NULL DEFAULT 0,          -- add count
  synced_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (slug, source, tenant_id)
);

CREATE TABLE catalog_entry_versions (
  slug           TEXT NOT NULL,
  version        TEXT NOT NULL,                          -- semver
  source         TEXT NOT NULL,
  tenant_id      TEXT,
  bundle_tar     BYTEA NOT NULL,                         -- the entry contents
  digest         TEXT NOT NULL,                          -- sha256 of bundle_tar
  published_at   TIMESTAMPTZ NOT NULL,
  deprecated_at  TIMESTAMPTZ,
  PRIMARY KEY (slug, version, source, tenant_id),
  FOREIGN KEY (slug, source, tenant_id)
    REFERENCES catalog_entries(slug, source, tenant_id) ON DELETE CASCADE
);

CREATE TABLE catalog_entry_ratings (
  slug         TEXT NOT NULL,                            -- only meaningful for source='movate'
  tenant_id    TEXT NOT NULL,                            -- who rated (customer tenant)
  rating       SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
  comment      TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (slug, tenant_id, created_at)
);
```

The schema lives behind the `StorageProvider` Protocol (CLAUDE.md rule 6).
Postgres is the production backend; SQLite implements the same surface for
local dev / test (the in-memory double for unit tests).

## API surface

All endpoints are under `/api/v1/catalog/...` on the **customer runtime**
(ADR 032 compat contract). The Movate-operated `catalog.movate.io` exposes
the **same shape** under `/v1/catalog/...` for sync — read-only from the
customer's perspective. Scopes follow ADR 013.

| Method | Path | Scope | Purpose |
| ------ | ---- | ----- | ------- |
| `GET`  | `/api/v1/catalog/agents?tag=&shape=&q=&source=` | `read`  | List; tenant sees `movate` + their `private` + (later) `community`; server-side filter |
| `GET`  | `/api/v1/catalog/agents/{slug}`                  | `read`  | Entry detail (latest version + summary) |
| `GET`  | `/api/v1/catalog/agents/{slug}/versions`         | `read`  | Version history |
| `POST` | `/api/v1/catalog/agents`                         | `admin` | Submit a tenant-private entry; future: community (with moderation) |
| `POST` | `/api/v1/catalog/agents/{slug}/publish`          | `admin` | Promote a draft version to `latest_version` |
| `POST` | `/api/v1/catalog/agents/{slug}/ratings`          | `write` | Rate a Movate-curated entry |
| `POST` | `/api/v1/catalog/sync`                           | `admin` | Trigger an immediate sync from `catalog.movate.io` |

All responses are JSON; OpenAPI contract test (ADR 032) covers them.

## Hosting story — `catalog.movate.io`

A small Movate-operated FastAPI service backed by managed Postgres in
**Movate's tenant** (ADR 039 — Movate-side hosted services). It reuses
MDK's own runtime code paths for `GET /v1/catalog/...` (the same handlers
serve customer runtimes), with **no tenant-scoped state** of its own — the
service only serves the `movate` namespace. Writes are gated to Movate
engineering (publishing happens via `movate/agent-catalog` CI, not over the
public API). Typical sync footprint: **~50 MB** of cached manifests +
bundles per customer; incremental sync via watermark keeps ongoing transfer
small.

## Data sovereignty (what crosses the customer boundary)

A customer runtime's outbound flow to `catalog.movate.io` carries:
- The **watermark timestamp** (last successful sync);
- Standard HTTP metadata (TLS, user-agent containing MDK version);
- Optionally, an org-attribution token (**Open question 1**) — only if the
  customer opts in to per-org ratings / analytics. Default is anonymous.

What never crosses:
- Tenant identity (unless opted in for attribution);
- Tenant-private entries (`source = "private"` rows stay in customer
  Postgres — there is **no upstream endpoint** that accepts them);
- Tenant data (KB rows, runs, traces, evals — out of scope here).

This matches ADR 018 / ADR 022's tenant-data-stays-in-tenant posture.

## Consequences

**Positive**
- Mova iO's "Agent Marketplace" surface has a backing service it can ship
  against (`/api/v1/catalog/...`).
- The gallery stops being frozen at image-build time — Movate can ship new
  starters between MDK releases.
- Customers get a sanctioned reuse pattern for **their own** internal
  templates (the tenant-private namespace).
- The community story has a schema-ready slot — no rewrite needed when the
  moderation ADR lands.
- Air-gapped customers degrade cleanly (bundle floor + `import-bundle`).

**Negative / risks**
- Movate now **operates a service customers depend on** for live catalog
  updates. The bundle floor mitigates outages; SLO + on-call posture is
  ADR 039's job.
- A second source of truth for "what agents exist in a tenant" (the catalog
  cache vs. the deployed registry, ADR 014). **D6** makes the boundary
  explicit: catalog = reusable templates; registry = actual deployed agents;
  add = one-way clone.
- Schema drift between `catalog_entries` and `agent.yaml` is possible
  if MDK evolves faster than the catalog. Mitigation: the catalog entry's
  bundle is opaque to the catalog service (it's a tar); MDK's loader is
  the authority on shape at add-time.

## Alternatives considered

- **GitHub-direct pull.** Customer runtime pulls
  `https://github.com/movate/agent-catalog` directly. No Movate service to
  operate. Rejected for v1 because it loses analytics / ratings / server-side
  search / fast incremental sync, leaks Movate's release cadence to customer
  firewalls, and forces every customer to allow GitHub egress.
- **Pure-bundled gallery (status quo).** No customer egress, no service to
  operate. Rejected because the gallery stays frozen at image-build time —
  the exact gap this ADR addresses.
- **Central marketplace + customer wallet / billing.** A full Stripe-backed
  marketplace from day one. Rejected as over-engineered for v1 — the
  immediate need is a curated catalog and a tenant-private reuse pattern;
  community pricing is a separate problem.

## Open questions (for Deva)

1. **AuthN on `catalog.movate.io`.** Anonymous read (no identity), signed
   org-attribution token (per-org ratings/analytics), or full Entra ID
   (deeper coupling)? Recommendation: **anonymous read by default**, opt-in
   org-attribution token for ratings.
2. **Versioning convention.** Catalog entries are reusable templates, not
   the MDK CalVer system — recommend **SemVer** (`MAJOR.MINOR.PATCH`) for
   `catalog_entry_versions.version`. Confirm.
3. **Versioning of the catalog API itself.** `/v1/catalog/...` —
   **additive evolution only** (CLAUDE.md rule 5). Confirm we never go to
   `/v2/catalog`.
4. **CLA + moderation for community.** Defer to a separate ADR (recommended)
   or design now? Schema is ready either way.
5. **Air-gapped flow.** Provide `mdk catalog export` (Movate side, packages
   a snapshot) + `mdk catalog import-bundle` (customer side, sideloads the
   snapshot) as the offline path. Confirm.

## Boundaries (out of scope)

- **Nested catalogs / categories** beyond the `tags` array (defer).
- **Discovery beyond text search + tag filter** (defer ML-based
  recommendations).
- **Pricing / licensing of community submissions** — separate ADR with the
  moderation policy.
- **Federation across Movate tenants** (multi-Movate-tenant cross-catalog
  sync) — explicitly out (ADR 038 "Agentic Mesh / Organizational" boundary
  applies).
- **Re-sync of consumer agents on catalog update** — **declined by D6**
  (catalog → registry is one-way, by design).
- No changes to the existing bundled-template gallery (`src/movate/templates/`)
  in this ADR; that remains the floor.
