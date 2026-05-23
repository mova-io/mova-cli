# ADR 014 — Durable shared agent registry: publish agents without rebuilding the image

**Status:** Proposed
**Date:** 2026-05-23
**Deciders:** Engineering (storage + deployment-lifecycle — structural; CODEOWNER review required)
**Context window:** v1.0 Azure operability — async + team agent authoring
**Builds on / related:** ADR 013 (scopes gate *who* may publish), ADR 009 (pgvector KB storage — KB stays there), ADR 001 (cloud-portability),
the jobs/worker infra (`JobKind`, `dispatch.py`), `src/movate/promotions/store.py`,
`src/movate/runtime/app.py` (`app.state.agents` / `agents_path` / `persist_bundle` / `scan_agents`),
`src/movate/core/loader.py` (`load_agent`), `src/movate/storage/base.py` (`StorageProvider`),
`src/movate/cli/deploy.py`

---

## Decision

Make the **deployed** agent registry **durable + shared** by storing agent
bundles behind the `StorageProvider` Protocol (not the API pod's local
filesystem), and **decouple two things that are conflated today**:

1. **Publishing an agent** — an **instant, durable registry write** (`PUT/POST
   /agents` → storage), dynamically loaded by every pod. No image rebuild.
2. **Deploying the platform** — the rare `mdk deploy` container-image rebuild,
   only when code/deps/the runtime itself change.

Concretely:

1. **(D1) Bundle-as-row registry.** New `StorageProvider` methods persist a
   versioned agent bundle (the small text files: `agent.yaml`, `prompt.md`,
   schemas, dataset, skills, contexts) as a serialized blob/JSONB row, tenant-
   scoped, on all backends (sqlite / postgres / in-memory double). KB is **not**
   in the bundle — it already lives in pgvector storage (ADR 009).
2. **(D2) Runtime resolves agents from the registry**, with a version-keyed
   per-pod materialization cache — so the **API and worker pods see the same
   agents**, surviving recycles and multi-replica scale-out. The
   filesystem/`agents_path` path becomes **local-dev + an import seed**, not the
   deployed source of truth.
3. **(D3) Versioned, with history + rollback + optimistic concurrency**, so a
   team can edit collaboratively without clobbering and can revert.

One sentence: **"deployed agents live in durable shared storage, so publishing
an agent is an instant API write that every pod (incl. the async worker) sees —
image rebuilds are reserved for platform changes — with versions, history,
rollback, and concurrency-safe team edits."**

---

## Context

Today the deployed runtime keeps agents in an **in-memory list
(`app.state.agents`) populated from a pod-local filesystem (`agents_path`)**;
`POST/PUT /agents` calls `persist_bundle()` to write `<agents_path>/<name>/` and
`scan_agents()` to reload. There is **no durable agent record in storage**. That
produces four problems that block async + team authoring on Azure:

* **Async is broken (BACKLOG #109).** An agent created via the API lands on the
  **API pod's** filesystem; the **worker pod** (which runs async run/eval/bench
  jobs) can't see it — so async execution of a just-published agent fails unless
  `?wait=true` runs it *inline on the API pod*. The async path is the whole
  point for a team.
* **Not multi-replica safe.** With ACA/KEDA scaling the API to N replicas, an
  agent published to replica A is invisible to replica B.
* **Ephemeral.** A pod recycle loses unpublished/uncommitted agents.
* **No audit / rollback.** No record of who published which version when; no
  durable history to revert to.

And `mdk deploy` conflates "ship the platform" with "ship an agent": it's a
**synchronous, multi-minute** `az acr build` + `containerapp update` +
`/healthz` poll — far too heavy for the routine act of editing a prompt.

The fix is a model shift, not a feature: agents must live in the **same durable
store the rest of the runtime already uses**, and publishing must be a cheap
data operation. This ADR is that shift. It is the keystone the async/team
authoring polish (remote hot-reload, async eval-gate, async deploy jobs,
promotion) all build on.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Async correctness** — the worker must see what the API published (close #109) | HIGH |
| **Multi-replica + recycle durability** — the registry can't be pod-local | HIGH |
| **Backward compatibility** — `mdk serve --agents ./dir` (local), the `/api/v1` agent contract, and `load_agent` must keep working | HIGH |
| **Cloud portability (ADR 001)** — store via the `StorageProvider` Protocol (sqlite/postgres), no new cloud-specific dependency | HIGH |
| **Team ergonomics** — versions, history, rollback, concurrency-safe edits, instant publish | HIGH |
| **Operability** — publishing a prompt edit should be seconds (data write), not a 5-minute image rebuild | MED |

---

## Architecture

```
  BEFORE                                   AFTER
  ──────                                   ─────
  PUT /agents                              PUT /agents  (scope-gated, ADR 013)
     └─ persist_bundle →                      └─ save_agent_bundle → STORAGE
        <agents_path>/<name>/ (API pod fs)        (versioned row, tenant-scoped)
        app.state.agents (in-mem, that pod)              │
                                                         ▼  every pod resolves:
  worker pod: can't see it (#109)          API pod ─┐   get_agent_bundle(name,ver)
  replica B: can't see it                  worker  ─┼─▶  → materialize to a
  recycle: lost                            replica ─┘     version-keyed cache dir
                                                          → load_agent (unchanged)

  "deploy an agent" == rebuild image       "publish an agent" == storage write (instant)
  (az acr build + containerapp update)     "deploy the platform" == mdk deploy (rare,
   ~minutes, synchronous                     only when code/deps/runtime change)
```

The seam already exists: `StorageProvider` (durable, multi-backend), `load_agent`
(reads bundle files), and the in-memory `app.state.agents` (becomes a cache).

---

## Decisions

### Decision 1 (D1): Bundle-as-row, behind `StorageProvider` — no new infra

Add `save_agent_bundle` / `get_agent_bundle(name, *, tenant_id, version=None)` /
`list_agents(*, tenant_id, …)` / `delete_agent_bundle` to the Protocol + sqlite +
postgres + the in-memory double. A bundle's files are **small text** (yaml / md /
json / jsonl / skill files) — serialize them into a single JSONB/blob column
(content-addressed by a bundle hash) plus columns for `name`, `tenant_id`,
`version`, `created_by`, `created_at`. **KB stays out of the bundle** — it's
large and already durable in pgvector (ADR 009); the registry records *which* KB
the agent expects, not the chunks. This needs **no object store** (works on
sqlite local + postgres prod); an S3-API blob backend can slot behind the same
methods later if bundles ever grow large (ADR 001 allows it behind an adapter).

Rejected: object storage now (premature infra for KB-sized payloads we don't
have); a brand-new service (the existing storage layer is the right home).

### Decision 2 (D2): Runtime resolves from the registry; filesystem becomes dev + seed

`app.state.agents` becomes a **read-through cache** of the durable registry, not
the source of truth. On resolve (API or worker), look up
`get_agent_bundle(name, tenant_id)`; **materialize** the bundle files to a
**version-keyed per-pod cache dir** (`<tmp>/mdk-agents/<name>/<version>/`) on
first use, then call the **unchanged `load_agent`** (it still reads files — no
loader rewrite). Because the cache key includes the version, a newly published
version is a natural cache miss → every pod picks it up without cross-pod
invalidation machinery. Local `mdk serve --agents ./dir` still scans a directory
(dev/offline), and that directory is also the **one-time import seed** for the
deployed registry.

Rejected: rewriting `load_agent` to take in-memory bundles (large blast radius);
a cross-pod pub/sub cache-invalidation bus (version-keyed materialization makes
it unnecessary).

### Decision 3 (D3): Versioned, with history, rollback, and optimistic concurrency

Every publish creates a **new immutable version** (bump the bundle's `version`);
retain the last *N* versions. Add:
- **Optimistic concurrency** — `PUT /agents` takes the expected current version
  (`If-Match`/ETag); a stale write is rejected (409) so two teammates can't
  silently clobber.
- **History + rollback** — `GET /agents/{name}/versions` and a `revert`
  (BACKLOG #80) that re-publishes a prior version forward (never destroys
  history). `mdk agent history` / `mdk agent revert` surface it.
- **Audit** — `created_by` (from the auth identity / ADR 013) + `created_at` on
  every version → "who published what when."

### Decision 4 (D4): Publishing is scope-gated (interlock with ADR 013)

Publishing — especially to a prod tenant/env — requires the `admin`/publish
scope from ADR 013's authorization model; read/run require lesser scopes. The
registry write is where least-privilege is enforced, so "who can publish to
prod" is a first-class control, not a shared-key free-for-all.

### Decision 5 (D5): Backward compatibility — local dev + the wire contract unchanged

- `mdk serve --agents ./agents` (local/offline) keeps scanning the filesystem;
  the durable registry is the **deployed** runtime's store.
- The `/api/v1` agent **request/response shapes** are unchanged; only the
  *persistence backend* behind `POST/PUT/GET /agents` moves from filesystem to
  storage (additive new storage methods; existing routes/schemas intact).
- A **one-time import** migrates any existing filesystem agents into the registry
  on first boot (idempotent), so deployed agents aren't lost.

### Decision 6 (D6): This closes the async gap and reserves image rebuilds for the platform

With the worker reading the same registry, async run/eval/bench of a
freshly-published agent **just works** — `?wait=true` stays as a sync
convenience, not a workaround. `mdk deploy`'s image rebuild is reserved for
**platform** changes (code/deps/runtime); routine agent publishing no longer
touches ACR or restarts a container. (The *async deploy job* + deployment record
for platform deploys is a separate, complementary item — out of scope here.)

---

## Consequences

**Positive**
- **Async authoring works** end-to-end (worker sees published agents); #109 dissolves; multi-replica + recycle safe.
- **Publishing a prompt edit is seconds** (a storage write), not a multi-minute image rebuild — the routine team loop gets ~100× faster and is observable.
- **Versions, history, rollback, concurrency-safe edits, audit** — the collaboration primitives a team needs.
- Portable (no new cloud dep); least-privilege via ADR 013; KB unaffected.

**Negative / costs**
- A real storage-schema addition (new table + methods on every backend + the double) — gated, needs CODEOWNER review + a careful import migration so no deployed agent is lost.
- A resolution path that reads storage + materializes files (mitigated by the version-keyed cache); a cold cache adds one storage read + a file write per (agent, version) per pod.
- Two "deploy" concepts to teach (publish-an-agent vs. deploy-the-platform) — a docs/UX clarity task.

**Neutral**
- New storage methods + an `agents`/`agent_versions` table; `app.state.agents` reframed as a cache. All additive; the wire contract and local-dev path are unchanged.

---

## Implementation plan (separate PRs, after this ADR is accepted)

1. **Storage layer** — `save_/get_/list_/delete_agent_bundle` on the Protocol +
   sqlite + postgres + in-memory double; the `agents` (+ `agent_versions`) table;
   conformance tests over all backends (PG gated). No behavior change yet.
2. **Runtime resolve-from-registry** — `app.state.agents` → read-through,
   version-keyed materialization cache; `POST/PUT/GET /agents` persist to + read
   from storage; the one-time filesystem→registry import. Keep `mdk serve
   --agents` (local) working. **This is the PR that closes #109** — add a test
   that an agent published via the API is runnable by the worker.
3. **Versioning UX** — optimistic concurrency (If-Match/409), `GET
   …/versions`, `revert` (#80), `mdk agent history|revert`, `created_by` audit.
4. **Scope gating** (after ADR 013 L2 lands) — require the publish scope on
   write paths.
5. **Docs** — "publish an agent vs. deploy the platform"; update `mdk dev` /
   `mdk deploy` guidance; the import/migration runbook.

## Risks / open questions

- **Cache coherence across pods** — version-keyed materialization avoids a
  pub/sub bus, but "resolve latest" (no explicit version) still needs a fresh
  storage read (cheap) or a short TTL; validate the read-per-resolve cost under
  load (it's one indexed lookup, comparable to the existing per-request
  `get_api_key`).
- **Bundle size** — fine as a row for text bundles; if a future bundle embeds
  large assets, move files to an S3-API blob behind the same methods (ADR 001).
- **Import migration** must be idempotent + non-destructive (never drop a
  filesystem agent that didn't import cleanly).
- **Skills with side effects / native code** in a bundle — the registry stores
  declarative skill specs + files; executing them is unchanged (the executor +
  side-effects policy from prior ADRs still gate).
