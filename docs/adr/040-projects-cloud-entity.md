# ADR 040 — Projects as a first-class cloud entity

**Status:** Accepted — shipped (projects as a cloud entity; /api/v1 projects). _(status reconciled to shipped reality 2026-06-08)_
**Date:** 2026-05-28
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x — close the "what is a Project" gap between the
local-authoring model (`project.yaml` on disk, ADR 026) and the cloud runtime,
so the Mova iO front end can drive end-to-end project lifecycle over `/api/v1`
without ever touching a workstation.
**Builds on / related:** ADR 014 (durable shared agent registry — what we now
*group* into Projects), ADR 013 (end-to-end identity + tenant/least-privilege
scopes — Project RBAC composes onto this), ADR 018 (per-tenant BYOK — Projects
inherit tenant credentials by default), ADR 026 (`mdk init` front-door UX — the
local `project.yaml` whose semantics we now mirror server-side), ADR 032
(front-end `/api/v1` completion — these endpoints extend that surface), ADR 039
(Lighthouse-style delegation — precedent for *delegated* governance, reused at
the Project tier).
**Related but separate work (do NOT bundle):** ADR 041 (agent catalog — the
"add agent from catalog" path), and the immediate **unified KB ingest**
endpoint PR (`POST /api/v1/kb/ingest` consolidating file/URL/multipart paths) —
both referenced from this ADR but decided elsewhere.

---

## Context

A **Project** today is a *local* construct. `mdk init` scaffolds a
`project.yaml` on disk; the directory groups agents, workflows, KBs, and
contexts for the author. The cloud runtime, however, has **no Project
container** — it knows about agents (ADR 014's durable registry) and workflows
individually, both tenant-scoped, but nothing groups them.

This breaks several scenarios the Mova iO front end (and any
**API-only consumer**) needs:

- **Team workspaces** — "show me everything Team A owns" cannot be answered:
  agents/workflows are flat under the tenant.
- **Mixed authoring paths** — a user wants to **create a Project, then add
  agents** to it by (a) describing them in English (reusing the ADR 025/032
  scaffold-preview path), (b) picking from a catalog (ADR 041), or (c)
  uploading a multipart bundle. Today each path lands an *unaffiliated* agent.
- **Scoped KBs and contexts** — KBs are tenant-global; there is no way to say
  "this KB belongs to *this* project" without leaking it across the tenant.
- **Project-scoped collaboration** — an *owner* who is not a tenant admin must
  be able to invite collaborators *to their project only*. Today the only
  granularity is tenant scopes (ADR 013), which is too coarse.

A "Project" is the natural grouping enterprises already think in. Making it a
**first-class cloud entity** lets the front end ship a project-centric UX, lets
RBAC delegate cleanly, and gives the runtime a stable home for cross-cutting
resources (KBs, contexts, future settings/budgets) that don't belong on an
individual agent.

## Decision

Introduce a **tenant-scoped Project entity** as a first-class container in the
runtime, with a member/role model layered on top of ADR 013 scopes. Agents,
workflows, KBs, and contexts gain a **many-to-many** relationship to Projects
(except KBs, which are project-scoped by default with explicit share-out — see
D3). All changes are **additive and back-compatible** via D5's
`tenant_default` project.

- **D1 — Tenant-scoped `Project` entity with owner + members.** A new
  `projects` row keyed by `(tenant_id, project_id)`, with `name`, `slug`,
  `description`, `owner_user_id`, `created_at`, `archived_at` (nullable).
  Owner is *not* required to be a tenant admin; the project is *scoped within*
  the tenant. Membership lives in `project_members(project_id, user_id, role,
  invited_by, joined_at)`.
- **D2 — Agents and workflows are M:N with Projects.** A single agent or
  workflow can belong to multiple projects within the same tenant (e.g. a
  shared "RAG helper" agent attached to several team projects). The
  attachments live in `project_agents` / `project_workflows`. The agent's own
  ownership (ADR 014's tenant-scoped registry row) is unchanged; Project
  attachment is a *membership* relation, not a re-parenting.
- **D3 — KBs are project-scoped by default; explicit cross-project share.**
  A KB is created **under exactly one Project** (its owning project). By
  default it is visible only to members of that project. To make it usable in
  another project within the same tenant, the owning project's owner/editor
  attaches it via `project_kbs(project_id, kb_id, mode='read')` — a
  **reference share**, not a copy. **Copy-on-attach** is supported as an
  override (`mode='copy'`) for cases where the consuming project needs an
  independent lifecycle (re-ingest, divergent chunking). KBs are *never*
  silently tenant-global.
- **D4 — Project-level RBAC wraps the existing scope model.** Three project
  roles: **viewer** (read project + its resources), **editor** (CRUD on
  agents/workflows/KBs/contexts *attached to* the project), **owner**
  (membership + delete/archive). Project roles are evaluated **per-resource
  at runtime** and **composed with tenant scopes** as a union: a user's
  effective permission on a resource = `tenant_scopes ∪ project_role_grants`
  where the resource is attached to a project they're a member of. Concretely:
  a project owner *may grant* `project:editor` on their project to a user who
  only holds `tenant:read` — and that user gains edit rights *only* on
  resources attached to that project. Existing tenant scopes (ADR 013) remain
  the floor and are never weakened by project roles.
- **D5 — Legacy agents auto-join a per-tenant `default` project; no
  force-migrate.** On first read after the migration, any agent/workflow
  without a `project_agents` / `project_workflows` row is implicitly attached
  to a single, tenant-owned **`<tenant>-default`** project (created lazily).
  The default project is a normal Project in every respect except that it
  cannot be deleted while the tenant exists. Users may move resources to
  other projects at any time; nothing is force-migrated.
- **D6 — Project deletion is soft.** `DELETE /api/v1/projects/{id}` sets
  `archived_at` and detaches members; the project disappears from listings but
  **agent versions, KB chunks, and run history are preserved** (the durable
  registry, ADR 014, and run tables are unchanged). Hard-delete is a separate,
  admin-only purge with a retention window — not in scope for this ADR.

## Schema additions (DDL-ish — illustrative, behind `StorageProvider`)

All tables carry `tenant_id NOT NULL` and are filtered by it on every read,
consistent with the existing multi-tenant invariants (ADR 013/014/018). The
concrete migration lands in a follow-up implementation PR.

```text
projects (
  project_id          TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  slug                TEXT NOT NULL,                -- unique within tenant
  name                TEXT NOT NULL,
  description         TEXT,
  owner_user_id       TEXT NOT NULL,
  is_default          BOOLEAN NOT NULL DEFAULT 0,   -- one per tenant (D5)
  created_at          TIMESTAMP NOT NULL,
  archived_at         TIMESTAMP,                    -- soft delete (D6)
  UNIQUE (tenant_id, slug)
)

project_members (
  project_id          TEXT NOT NULL,
  user_id             TEXT NOT NULL,
  tenant_id           TEXT NOT NULL,                -- denormalized for filter
  role                TEXT NOT NULL CHECK (role IN ('viewer','editor','owner')),
  invited_by          TEXT NOT NULL,
  joined_at           TIMESTAMP NOT NULL,
  PRIMARY KEY (project_id, user_id)
)

project_agents (
  project_id          TEXT NOT NULL,
  agent_id            TEXT NOT NULL,
  tenant_id           TEXT NOT NULL,
  attached_at         TIMESTAMP NOT NULL,
  attached_by         TEXT NOT NULL,
  PRIMARY KEY (project_id, agent_id)
)

project_workflows (
  project_id          TEXT NOT NULL,
  workflow_id         TEXT NOT NULL,
  tenant_id           TEXT NOT NULL,
  attached_at         TIMESTAMP NOT NULL,
  attached_by         TEXT NOT NULL,
  PRIMARY KEY (project_id, workflow_id)
)

project_kbs (
  project_id          TEXT NOT NULL,
  kb_id               TEXT NOT NULL,
  tenant_id           TEXT NOT NULL,
  mode                TEXT NOT NULL CHECK (mode IN ('owned','read','copy')),
  attached_at         TIMESTAMP NOT NULL,
  attached_by         TEXT NOT NULL,
  PRIMARY KEY (project_id, kb_id)
)
```

Notes:
- `project_kbs.mode='owned'` exists on the owning project's row (one per KB);
  `mode='read'` is the reference share (D3); `mode='copy'` triggers a separate
  KB row with its own ingest lifecycle.
- Contexts attach the same way as agents (`project_contexts` mirrors
  `project_agents`); omitted above for brevity, identical shape.

## API surface (additive under `/api/v1`)

All endpoints honor the composed RBAC of D4. Scopes shown are the
*tenant-scope* floor; project-role checks layer on per-resource.

| Endpoint                                               | Method | Scope (floor)   | Notes |
| ------------------------------------------------------ | ------ | --------------- | ----- |
| `/projects`                                            | GET    | `tenant:read`   | List projects the caller is a member of (plus default). |
| `/projects`                                            | POST   | `tenant:write`  | Create; caller becomes owner. |
| `/projects/{id}`                                       | GET    | project viewer  | |
| `/projects/{id}`                                       | PATCH  | project editor  | Rename/describe. |
| `/projects/{id}`                                       | DELETE | project owner   | Soft (D6). |
| `/projects/{id}/members`                               | GET    | project viewer  | |
| `/projects/{id}/members`                               | POST   | project owner   | Invite / set role. |
| `/projects/{id}/members/{user_id}`                     | DELETE | project owner   | Remove. |
| `/projects/{id}/agents`                                | GET    | project viewer  | Attached agents. |
| `/projects/{id}/agents`                                | POST   | project editor  | Attach an existing agent (by id). |
| `/projects/{id}/agents/{agent_id}`                     | DELETE | project editor  | Detach (does NOT delete the agent). |
| `/projects/{id}/workflows`                             | GET/POST/DELETE | project viewer/editor | Mirror of agents. |
| `/projects/{id}/contexts`                              | GET/POST/PATCH/DELETE | project viewer/editor | CRUD on project-scoped contexts. |
| `/projects/{id}/kbs`                                   | GET    | project viewer  | List owned + shared-in KBs. |
| `/projects/{id}/kbs/{kb_id}/share`                     | POST   | project owner of owning project | Reference-share to another project; body: `{target_project_id, mode}`. |

**Agent-creation paths attaching to a Project** (reusing existing surfaces — no
duplication):

- **NLP / scaffold-preview** — reuse the ADR 025/032 draft-preview endpoint;
  the caller passes `project_id` so the resulting durable-registry row is
  attached on commit.
- **Catalog** — defined by **ADR 041**; this ADR only commits to honoring
  `project_id` on the catalog instantiation call.
- **Multipart bundle** — existing `POST /agents` (ADR 014) gains an optional
  `project_id` form field; absent → the caller's default project.

**KB ingestion** — **explicitly delegated**. The unified
`POST /kb/ingest` (file/URL/bundle) is decided in a separate immediate PR. This
ADR commits only to the *attachment surface* (`/projects/{id}/kbs/...`) and to
the rule that ingest creates a KB **under exactly one owning project** (D3).

## RBAC interaction (composing project roles with tenant scopes)

Authorization is evaluated **per-resource**. Pseudocode:

```text
def allowed(user, action, resource):
    tenant_ok  = action in scopes(user, resource.tenant_id)
    if tenant_ok:
        return True                                  # tenant scope wins
    # else: try project-role grant on any project the resource is attached to
    for p in projects_attached(resource):
        if user in members(p) and role(user, p) >= required_role(action):
            return True
    return False
```

Worked example: alice holds `tenant:read` only. The owner of project `gamma`
invites alice as `project:editor` on `gamma`. Alice can now `PATCH` an
agent attached to `gamma`, but the same agent attached *also* to project
`delta` (where alice is not a member) is invisible to her via the `delta`
listing — she edits it only through the `gamma` lens. The tenant-scope floor
is never weakened: a user with `tenant:write` keeps full access regardless of
project membership.

This reuses ADR 039's precedent of **delegated authority within a bounded
scope** (Lighthouse delegates to Movate within a customer tenant; project
owners delegate to members within a project).

## Migration — zero-downtime

1. Ship `projects`, `project_members`, `project_agents`, `project_workflows`,
   `project_kbs`, `project_contexts` as additive tables. No existing column is
   modified.
2. On startup (or lazily on first project read for a tenant), create the
   tenant's **`<tenant>-default`** project if absent, owned by a synthetic
   `tenant-system` principal (cannot be removed).
3. **No backfill of attachment rows.** D5's read path treats *missing*
   attachment as *implicitly in the default project*; existing API behavior
   that lists "all agents in tenant" continues to work unchanged.
4. New writes (create / attach) populate the new tables; old writes that
   omit `project_id` continue to land unaffiliated and are read as
   default-project members.
5. A later, **opt-in** maintenance job may materialize the implicit default
   attachments into explicit rows — purely for query-plan consistency, with no
   behavioral change.

No `/api/v1` endpoint is removed or changes shape; the `project_id` field is
optional everywhere it appears.

## Consequences

**Positive.**
- The Mova iO front end can ship a project-centric UX (workspaces, member
  invites, project dashboards) without further runtime changes.
- Owners can delegate without being tenant admins — collaboration scales below
  the tenant tier (the ADR 013 gap).
- KBs gain a natural ownership story (D3) and stop being accidental tenant
  globals.
- Run history, eval results, and reports (ADR 031/032) gain a `project_id`
  filter for free.
- Catalog (ADR 041) and unified ingest (separate PR) plug in cleanly via the
  `project_id` parameter.

**Negative / risk.**
- Any **cross-project shared resource** must now carry an explicit "shared"
  flag (`project_kbs.mode='read'`, M:N agent attachment). Implicit sharing via
  "it's in the same tenant" is gone.
- Authorization becomes per-resource (composed) instead of a single tenant
  scope check — measurably more work per request. Mitigation: cache the
  `(user, project) → role` lookup per request; the membership tables are tiny
  vs the agent registry.
- Two attachment surfaces (catalog vs scaffold-preview vs multipart) must all
  honor `project_id` consistently; contract test required.

## Alternatives considered

- **Flat tenant (status quo).** Cheapest; defers the problem. Rejected: blocks
  the Mova iO project UX and forces every front-end query to re-derive
  project membership client-side.
- **Nested projects (folders/sub-projects).** Matches some enterprise mental
  models but compounds the RBAC composition matrix and the M:N decisions.
  **Rejected for v1**; revisit only if a real customer scenario forces it.
- **Per-user workspaces.** Aligns with personal-tool UX (Notion-style) but
  miscasts MDK's audience: agents are team artifacts (eval datasets, run
  history, KBs). **Rejected** — Projects are team-oriented; a single-member
  project models "personal" cleanly enough.
- **Re-parent agents under projects (1:N, not M:N).** Simpler schema, but
  forecloses the common "shared utility agent across teams" case and forces
  copies. **Rejected**; D2 keeps the option open.

## Resolved decisions (locked 2026-05-28)

The five open questions surfaced during draft have been resolved with input
from the architectural review. All five resolutions hold the originally
proposed shape:

1. **Membership administration model — RESOLVED: owner-driven.**
   Project owners can invite any principal within the tenant. Tenant admins
   retain a force-add / force-remove backstop via their tenant-level scopes;
   they do not need to be invited per-project. This composes cleanly with
   ADR 013 — tenant admin's `admin` scope is the floor on every project.

2. **Nested projects — RESOLVED: flat for v1.**
   No project hierarchy in v1. A future ADR can revisit if a committed
   customer scenario emerges; D1 + D4 are structured so a future `parent_id`
   column is an additive migration, not a rewrite.

3. **Standalone (un-projected) agents — RESOLVED: allowed (D5 stands).**
   Legacy agents — and any agent created without an explicit `project_id` —
   auto-attach to a per-tenant `default` project on first read. This preserves
   ADR 014 back-compat verbatim. The default project cannot be deleted; it can
   be renamed.

4. **KB share semantics — RESOLVED: reference-share by default, copy-on-attach
   as an explicit override (D3 stands).**
   The default `POST /projects/{id}/kbs/share` attaches a read-only reference
   to the source KB. A future `copy=true` flag will materialize a forked KB
   (separate `kb_id`, independent chunk lifecycle) when a project needs to
   diverge from the upstream KB's content. The forked copy is its own owned
   resource of the new project.

5. **URL ingest crawl scope — RESOLVED: support both modes via the unified
   ingest endpoint.**
   The unified-ingest PR will support both single-page synchronous ingest
   (default, `recursive=false`) and multi-page asynchronous crawl
   (`recursive=true&max_depth=N`, runs as a background job, returns a job_id
   that the caller polls or subscribes to via SSE). The final endpoint shape
   is owned by the unified-ingest PR; this ADR commits only to the Project-KB
   attachment semantics that surround it.

## Boundaries (explicit scope-out)

- **Agent catalog** — owned by **ADR 041**. This ADR only commits to the
  catalog endpoint accepting `project_id`.
- **Unified KB ingest** (`POST /kb/ingest`) — owned by a separate immediate
  PR. This ADR commits to the attachment surface and the
  "exactly-one-owning-project" rule.
- **Hard delete / purge / data retention** for archived projects — separate
  retention policy ADR.
- **Quota/budget per project** — outside scope; the existing ADR 036 (usage
  metering/quotas) sits at the tenant tier today and would extend to projects
  in a follow-up.
- **Cross-tenant project sharing.** Explicitly **not supported**. Projects are
  bounded by `tenant_id` and never escape it. Cross-tenant federation, if
  ever, is a separate north-star.
- **CLI surface.** No CLI changes in this ADR; the local `project.yaml` model
  (ADR 026) remains the on-disk authoring artifact. A later ADR may align the
  CLI to push/pull against cloud projects, but it is not required for the
  front-end-only consumer this ADR enables.
