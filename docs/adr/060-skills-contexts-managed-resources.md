# ADR 060 — Skills & Contexts as first-class managed resources (full API + CLI parity)

**Status:** Accepted — shipped (skills/contexts as managed resources; #652 D4 managed-ref resolution). _(status reconciled to shipped reality 2026-06-08)_
**Date:** 2026-05-31
**Deciders:** Engineering (control-plane / API)
**Context window:** close the last real CLI↔API parity gaps — promote skills and
shared contexts from *bundle-only components* to *managed resources* with full
CRUD + agent-attach over `/api/v1`, matching projects / agents / KB.
**Builds on / composes with (changes nothing in any of them):**
ADR 002 (skills + shared contexts — the components themselves; bundle-local
authoring stays the floor),
ADR 014 (agent registry — `AgentBundleRecord` + storage methods +
resolve-from-registry + FS→registry import; **the precedent this ADR mirrors**),
ADR 040 (projects as a first-class cloud entity — the precedent for elevating a
bundle-adjacent thing to a managed, tenant-scoped resource),
ADR 041 (agent catalog — Movate-curated + tenant-private; skills/contexts become
catalog-publishable the same way, later),
the `StorageProvider` Protocol (the persistence seam — multi-backend
SQLite/Postgres), and the **CLI↔API parity gate** (`tests/test_cli_api_parity.py`,
which will enforce this once the verbs are mapped).

**Defining gap.** A parity audit of `main` shows **projects, agents, and KBs have
genuine full API↔CLI parity**, but the other two bundle components do not:

- **Skills:** only `POST /api/v1/skills` (create) exists — **no list, get,
  update, delete, or agent-attach** over the API. `mdk skills` does more.
- **Contexts:** **no `/api/v1` surface at all.** `mdk contexts` is **CLI-only.**

Skills and shared contexts were *designed* as **bundle components** (ADR 002) —
authored locally in the agent bundle (`skills/`, context files), then deployed.
That is correct for the author-locally-then-deploy flow and stays untouched. But
for the **hosted / multi-tenant** story — manage and share skills/contexts via
API without a local checkout, exactly as ADR 014/040/041 did for *agents* — they
need to be first-class managed resources. This ADR makes them so, **additively**.

This is a **design** ADR. It expands the `/api/v1` surface and promotes two
bundle components to managed, tenant-scoped, versioned resources behind the
`StorageProvider` Protocol — hence rule 2. The endpoints, storage methods,
client methods, and CLI `--target` wiring land in follow-up PRs (Boundaries).

---

## Context

Agents got the full treatment — a registry (ADR 014), cloud-entity projects
(ADR 040), a catalog (ADR 041) — so you can list/create/version/share/attach
*agents* over the API. Their two sibling bundle components didn't:

- A hosted operator can't **inventory a tenant's skills** (`GET /skills`),
  **inspect or update one** (`GET/PUT /skills/{name}`), **retire one**
  (`DELETE`), or **attach a registry skill to an agent** over the API. They can
  only *create* (`POST /skills`) — a half-resource.
- Shared **contexts** have **zero** API — they only exist as bundle files, so a
  hosted tenant can't manage the "company tone" / "policy preamble" contexts
  that ADR 002 made reusable across agents.

The result is a CLI-only management story for two resources the platform already
treats as reusable, versionable, and shareable everywhere *except* the API. That
blocks the hosted-playground / multi-tenant / catalog directions and is an
inconsistent seam (rule 6) for front-end integrators.

## Decision

Promote **skills** and **shared contexts** to first-class managed resources —
full CRUD + agent-attach over `/api/v1`, persisted behind `StorageProvider`,
tenant-scoped and versioned — **mirroring the agent registry (ADR 014)**.
Bundle-local authoring (ADR 002) remains the default and the portable floor.

### D1 — Managed records behind `StorageProvider` (mirror ADR 014)

Add `SkillRecord` and `ContextRecord` (name, tenant_id, version, the
component's canonical spec/content + metadata) and the `StorageProvider` methods
to persist/list/get/delete them — exactly parallel to `AgentBundleRecord` and its
methods (ADR 014.1). Multi-backend (SQLite + Postgres), tenant-scoped in every
query (rule 6). Versioned (skills already carry a version; contexts gain one),
so update is a new version with the same optimistic-concurrency story agents use
(ADR 014.3). `core`/`kb` depend on the Protocol, never a concrete backend.

### D2 — Complete the API surface (parity with projects/agents/KB)

| | Skills | Contexts |
|---|---|---|
| list | `GET /api/v1/skills` | `GET /api/v1/contexts` |
| create | `POST /api/v1/skills` *(exists)* | `POST /api/v1/contexts` |
| get | `GET /api/v1/skills/{name}` | `GET /api/v1/contexts/{name}` |
| update | `PUT /api/v1/skills/{name}` | `PUT /api/v1/contexts/{name}` |
| delete | `DELETE /api/v1/skills/{name}` | `DELETE /api/v1/contexts/{name}` |
| versions | `GET /api/v1/skills/{name}/versions` | `GET /api/v1/contexts/{name}/versions` |
| attach | `POST /api/v1/agents/{name}/skills` | `POST /api/v1/agents/{name}/contexts` |

Scopes: read on GET, `admin` (or a dedicated `manage`) on mutate — matching the
existing `POST /skills` (`admin`) and the agent/project routes. Standard
`--json` shapes + `_links` consistent with the other resources.

### D3 — Client + CLI `--target` parity

Add `MovateClient` methods for each, and give `mdk skills` / `mdk contexts` the
`--target` remote path (list/get/create/update/delete/attach) so the same verb
works locally (bundle) and against a runtime — exactly as `mdk agent` /
`mdk project` / `mdk kb` do. The **parity gate enforces it**: each new remote
verb maps to its route or the gate fails (it already xfails nothing here — these
become first-class).

### D4 — Registry resolution (mirror resolve-from-registry, ADR 014.2)

The runtime resolves an agent's skill/context **refs** from the managed store
when present, falling back to the bundle — the same precedence + FS→registry
import ADR 014.2 gave agents. So a deployed/hosted agent can reference a
registry skill/context by `name@version` instead of shipping a copy, and the
"author-locally" bundle path keeps working unchanged.

### D5 — Catalog-publishable later (compose with ADR 041)

Because skills/contexts are now records behind the same seam as agents, they can
be **catalog-published** (Movate-curated + tenant-private + community) the same
way agents are (ADR 041) — a future follow-up, not this ADR, but the record
shape (D1) is chosen so it's a thin addition rather than a rewrite. (The
"Prompt Library" backlog item is the contexts/prompts sibling of this.)

### D6 — Backward compatibility (additive, bundle stays the floor)

Bundle-local skills/contexts (ADR 002) are **unchanged** — an agent that ships
its own `skills/` + context files resolves them from the bundle exactly as
today. The managed store is an **additional** resolution source (D4) and an
**additional** management surface (D2). New tables are additive migrations; new
routes/verbs are additive (flagged per rule 5). No removal, no forced migration.

## Consequences

**Positive**
- **Closes the last real CLI↔API parity gaps** — skills become a full resource,
  contexts get an API; projects/agents/KB/skills/contexts are now uniform.
- Unblocks the **hosted / multi-tenant** management story and the **catalog**
  for skills/contexts (D5) — consistent with what agents already have.
- One consistent seam (StorageProvider + the registry pattern) for *every*
  bundle component, instead of agents-managed / skills-half / contexts-not.

**Negative / risks**
- **Two new storage schemas + a wider API** — additive migrations + new routes
  to test on all backends; mitigated by mirroring ADR 014's proven shape.
- **Two resolution sources** (bundle vs registry) — the precedence rule (D4) +
  FS→registry import must match agents' so behavior is predictable.
- **Scope creep toward a "everything is a registry" platform** — bounded by
  reusing the existing pattern and *not* inventing a generic artifact store
  (Alternatives).

## Boundaries

Skills/contexts records live behind the `StorageProvider` Protocol; `core`
depends on the Protocol, never a backend (rule 6/7). Control-plane (`cli`) ⊥
execution-plane; the runtime resolves refs at the edge. Additive, opt-in, bundle
authoring preserved. The CLI↔API parity gate is the enforcement. Mirrors ADR 014
/ ADR 040 rather than inventing a new seam.

## Alternatives considered

- **Keep skills/contexts bundle-only.** Rejected — blocks hosted/multi-tenant
  management + the catalog, and leaves a permanent CLI-only inconsistency the
  parity work is meant to eliminate.
- **A single generic "artifact" resource** for skills + contexts + prompts.
  Rejected — skills (executable, side-effect policy, schemas) and contexts
  (text/prompt fragments) have distinct shapes, validation, and scopes; one
  blob type loses the type-specific contracts ADR 002 defined.
- **Only finish skills (skip contexts).** Rejected — contexts are the *more*
  glaring gap (zero API), and doing them together keeps the seam uniform.
- **Catalog-first** (publish/share before CRUD). Rejected — you manage before
  you share; CRUD is the foundation, catalog (D5) is the layer on top.

## Scope / rollout

Multi-PR; this ADR is doc-only.

1. **Skills resource** — `SkillRecord` + storage methods (all backends) +
   complete the CRUD/attach/versions API (create exists) + `MovateClient` +
   `mdk skills --target` + parity-gate mapping.
2. **Contexts resource** — `ContextRecord` + storage + the full CRUD/attach/
   versions API (new) + client + `mdk contexts --target` + parity-gate mapping.
3. **Registry resolution** (D4) — resolve agent skill/context refs from the
   store with FS→registry import, mirroring ADR 014.2.
4. **Catalog publish** (D5) — skills/contexts into the ADR 041 catalog (future).
