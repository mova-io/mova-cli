# ADR 088 — Temporal worker loads published workflows from storage

- Status: Accepted
- Date: 2026-06-07
- Related: ADR 037 (workflow API parity / publish), ADR 054 (Temporal Track
  B/C), ADR 055 (runtime dispatch fork), ADR 078 (self-hosted Temporal)

## Context

The Temporal worker (`mdk worker --backend temporal`) registers workflows by
scanning a **filesystem path** (`scan_workflows(workflows_path)` →
`runtime: temporal` graphs). That is the only discovery source today.

This is a real deployment gap on Container Apps: a deployed worker's
`workflows_path` is an Azure Files volume, and **there is no automated path to
get a workflow definition onto that volume**. Empirically, the deployed
`movate-dev-temporal-worker` registers **0 workflows** ("the worker will start
but host nothing") even though `refund-approval` exists in the repo — so no
durable workflow can run or be inspected. Meanwhile agents/workflows are
*published to storage* via the API (ADR 037, `mdk workflow publish`), but the
worker doesn't read storage, so publishing doesn't make a workflow hostable.

## Decision

Give the Temporal worker a **second, opt-in discovery source: published
`runtime: temporal` workflows in storage** — so `mdk workflow publish <wf>`
followed by a worker (re)start hosts it, with no volume gymnastics.

### D1 — Gated + additive (default-off, zero regression)

A new `--from-storage` flag (env `MDK_TEMPORAL_WORKFLOWS_FROM_STORAGE=1`) on
`mdk worker --backend temporal`. **Default off** → the worker behaves exactly as
today (filesystem scan only). When on, storage-loaded workflows are **merged**
with the filesystem scan; the filesystem wins on a name collision (local-dev
override of a published def).

### D2 — Reuse the proven loader (no second parser)

`load_published_temporal_workflows(storage, tenant_id)`:
1. `storage.list_workflows(tenant_id, published_only=True)` (ADR 037).
2. For each, `get_workflow_bundle(...)` → its `files` map (the canonical
   `workflow.yaml` + `agents/*` + `schema/*`, ADR 037 `WorkflowBundleRecord`).
3. Materialize `files` to a temp dir (preserving relative paths) and run the
   **same** `scan_workflows()` the filesystem path uses — so there is ONE
   loader/compiler/validator, no divergent reconstruction.
4. Keep only `runtime: temporal` graphs.

Fail-soft per existing convention: a bad/uncompilable bundle is skipped with a
warning; the rest still load.

### D3 — Tenant scope (the worker is already tenant-aware)

`_run_temporal_worker` already carries a `tenant_id` (default `local`). Storage
loading is scoped to that tenant — the same tenant the worker's Executor runs
under. **Cross-tenant fan-out** (one worker hosting every tenant's published
temporal workflows) is explicitly **out of scope** here — it needs a
multi-tenant task-queue/isolation design and is a follow-up. This ADR ships the
single-tenant path that unblocks dev + single-tenant deployments.

## Consequences

- **Pro:** `publish → host` works — a durable workflow becomes inspectable
  (Temporal UI / `mdk workflow runs`) without writing to an Azure Files share.
  Closes the "0 workflows registered" gap.
- **Pro:** additive + gated → the filesystem default is byte-for-byte unchanged;
  one proven loader (D2).
- **Con / scoped:** single-tenant only (D3); multi-tenant fan-out deferred.
- **Con:** a worker restart is still required to pick up a newly-published
  workflow (no hot reload) — acceptable, matches the filesystem-scan model.

## Out of scope (follow-ups)
- Multi-tenant fan-out (host all tenants' published temporal workflows).
- Hot reload on publish (watch + re-register without restart).
- Seeding the filesystem volume from `mdk deploy` (the other half of the gap;
  this ADR makes storage the preferred path instead).
