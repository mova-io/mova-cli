# ADR 064 — Catalog-publish skills & contexts (a `kind` discriminator on the catalog)

**Status:** Accepted
**Date:** 2026-05-31
**Deciders:** Engineering (catalog / storage)
**Context window:** make the now-managed skills & contexts (ADR 060) **shareable
and discoverable** the same way agents are — a Movate-curated + tenant-private +
community catalog — so a tenant can browse, rate, and clone a `web-search`
skill or a `house-tone` context instead of re-authoring it. This is ADR 060 D5,
made concrete.
**Builds on / composes with (changes nothing in any of them):**
ADR 041 (the agent catalog — `CatalogEntry` + versions + ratings + sync + the
`/api/v1/catalog/agents*` surface; this ADR generalizes it), ADR 060 (skills &
contexts as managed `StorageProvider` records — the source rows a catalog entry
publishes; D5 named this follow-up and chose the record shape for it), and the
managed `SkillRecord` / `ContextRecord` (#650) that a publish reads from.

**Defining gap.** `CatalogEntry` (ADR 041 D2) is **agent-only** — it has a
`slug` / `source` / `latest_version` but **no `kind`**, so there is nowhere to
say "this catalog row is a *skill*." The full catalog machinery
(versions, ratings, namespaces, sync watermark, the curated card UI) is built
and proven for agents; the only thing stopping skills/contexts from reusing it
is the missing discriminator.

This is a **design** ADR for a storage-schema change (rule 2): one additive
column + a parameterized surface, rather than a parallel catalog.

---

## Decision

### D1 — Add a `kind` discriminator to the catalog, not a parallel table

Add `kind: "agent" | "skill" | "context"` to `CatalogEntry` (and its versions),
**defaulting to `"agent"`** so every existing row + the storage column + the
wire shape are backward-compatible (a pre-existing entry reads back as an agent
entry, byte-for-byte). One catalog table, three kinds — **not** three parallel
catalog/versions/ratings/sync stacks. Rationale: the entry/version/rating/sync
machinery is identical across kinds (a card, a semver history, a star rating, a
namespace); duplicating it three times is exactly the architectural entropy
CLAUDE.md warns against. The published payload per version differs by kind
(an agent bundle vs a `SkillRecord.files` vs a `ContextRecord.body`) and is
already a `files`/blob on the version row — so the *shape* is uniform, only the
*contents* vary.

### D2 — Parameterize the catalog surface by kind

The `/api/v1/catalog` routes gain the kind in the path, mirroring the agent
ones exactly:

| | Agents (exists) | Skills | Contexts |
|---|---|---|---|
| list | `GET /catalog/agents` | `GET /catalog/skills` | `GET /catalog/contexts` |
| detail | `GET /catalog/agents/{slug}` | `…/skills/{slug}` | `…/contexts/{slug}` |
| versions | `…/agents/{slug}/versions` | `…/skills/{slug}/versions` | `…/contexts/{slug}/versions` |
| ratings | `…/agents/{slug}/ratings` | `…/skills/{slug}/ratings` | `…/contexts/{slug}/ratings` |

The storage methods (`upsert_catalog_entry`, `list_catalog_entries`, …) gain a
`kind` parameter (default `"agent"`), tenant/namespace scoping unchanged.

### D3 — Publish & clone wired to the managed records

**Publish:** `POST /catalog/skills` (and contexts) snapshots the tenant's
managed `SkillRecord` / `ContextRecord` (#650) into a catalog entry+version
under the `private` namespace (or `movate` for curated). **Clone** is the
inverse of #650's attach path: cloning a catalog skill/context writes a
`SkillRecord` / `ContextRecord` into the cloning tenant's store (then D4
resolution makes it runnable). So the catalog is the *share* layer on top of
the *manage* layer — no new execution path.

### D4 — Backward compatibility (additive)

`kind` defaults to `"agent"` everywhere — existing rows, storage columns,
client/CLI, and the `/catalog/agents*` routes are **unchanged**. New
`/catalog/skills*` + `/catalog/contexts*` routes are additive (flagged, rule 5).
The new column is an additive `CREATE`/`ALTER … DEFAULT 'agent'` migration on
each backend, idempotent. The catalog sync job filters by kind so a curated
source can carry all three. No version bump in a PR (ADR 059).

## Consequences

**Positive**
- **One catalog** for every shareable artifact (agents/skills/contexts) — the
  same card, rating, version, namespace, and sync machinery, not three copies.
- Completes the ADR 060 arc: **author-locally → manage (record) → resolve & run
  (D4) → share (catalog)**. Skills/contexts become a marketplace, not just
  tenant-private rows.
- Thin: the record shape (ADR 060 D1) was chosen for this, so it's an additive
  column + parameterized handlers, not a rewrite.

**Negative / risks**
- A `kind` discriminator on a shared table means every query must filter on it —
  a missed filter could leak a skill into the agent catalog list; mitigated by
  the storage methods always taking `kind` (default `agent`) + a conformance
  test per kind.
- Curation/moderation for `movate`/`community` skills (executable code!) is a
  real trust surface — community-namespace *skills* (vs agents/contexts) carry
  side-effect risk; gate community-skill publish behind review (out of scope
  here, flagged for the rollout).

## Boundaries

Storage-schema change behind the `StorageProvider` Protocol (rule 6) — one
additive column, parameterized methods. Catalog is the share layer over the
ADR 060 manage layer; clone writes a managed record, never a bespoke store.
Reuses ADR 041 wholesale rather than inventing a second catalog.

## Alternatives considered

- **Parallel `skill_catalog` / `context_catalog` tables + duplicated
  routes/ratings/sync.** Rejected — triples the catalog machinery for zero
  semantic gain; the discriminator is one column.
- **A generic `artifact_catalog` collapsing agents too.** Rejected — a bigger
  migration that churns the proven agent catalog; additive `kind` reaches the
  same place with no risk to the shipped surface.
- **Skip the catalog; share via export/import files.** Rejected — loses
  discovery, ratings, versioning, and the curated story the agent catalog
  already proves customers want.

## Scope / rollout

1. `kind` column on `CatalogEntry` + versions (default `agent`) — additive
   migration, all backends + the double.
2. Parameterize the catalog storage methods + the `/catalog/{kind}*` routes (D2).
3. Publish/clone wired to `SkillRecord` / `ContextRecord` (D3), stacked on
   ADR 060 (#650) + D4 resolution (#652).
4. Community-skill publish review gate (trust) — follow-up.
