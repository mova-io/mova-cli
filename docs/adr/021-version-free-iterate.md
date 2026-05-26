# ADR 021 — Version-free iterate: redeploys propagate edits to the served agent

**Status:** Proposed
**Date:** 2026-05-25
**Deciders:** Engineering (deploy lifecycle + agent registry)
**Context window:** v1.0 inner loop — make "edit an agent → redeploy → see the new
behavior" actually work against a deployed runtime, the core promise of the MVP.
**Related / constrained by:** ADR 014 (durable agent registry — the
`AgentBundleRecord` store this resolves from), ADR 011 (`.mdk/` project state),
the `mdk deploy` agents-mode upload path.

## Decision

**The registry is the single source of truth for what a deployed agent runs, and
`mdk deploy` publishes a new immutable registry version whenever an agent's bundle
content changes — automatically, so an operator never has to hand-bump
`agent.yaml: version` to see an edit take effect.** "Latest published version for
`(name, tenant)`" is what runs by default; explicit version pinning still resolves
an exact version. Identical content re-published is an honest no-op, not a silent
"✓ uploaded".

This retires the on-disk agent *directory* as a run-resolution source: the
deploy's create/PUT-to-dir path no longer determines what executes.

## Context

A live end-to-end test on the current runtime surfaced that the MVP iterate loop
is **broken**: editing an agent's prompt and re-running `mdk deploy --target dev`
reports `✓ uploaded agent <name>`, but every subsequent run still executes the
**original** bundle. Reproduced on a fresh current-code runtime: local
`agent.yaml` = `0.1.1` with a changed prompt, deploy reports success, yet
`GET /api/v1/agents/<name>` returns `version 0.1.0` with the *original* prompt
hash, and runs return the old output. A manual version bump does **not** help.

Root cause — two stores, only one of which runs:

1. `mdk deploy` agents-mode (`_upload_one_agent_bundle`, `src/movate/cli/deploy.py`)
   does `POST /api/v1/agents` (create) and, on `409 already-exists`, **falls back
   to a PUT that replaces the on-disk bundle directory** (`agent_creation.py`).
2. But runs and `GET /api/v1/agents` resolve from the **registry** —
   `StorageProvider.save_agent_bundle` / `get_agent_bundle(version=None)` =
   *latest by `created_at`* (ADR 014, immutable per `(name, tenant_id, version)`).
3. The re-deploy PUT updates the *directory* but **never writes the registry**, so
   the served `AgentBundleRecord` stays frozen at first publish. A version bump
   doesn't escape this because the PUT path simply doesn't touch the registry at
   all.

Secondary defect found alongside: `GET /api/v1/agents/<name>?version=X` ignored
the `version` query param and returned latest.

The underlying tension: the registry is **immutable per `(name, version)`** (great
for audit, reproducibility, and rollback — a run pins exact content), but the most
common operator action is *iterating* ("I edited the prompt; show me the change"),
which naively wants overwrite-or-auto-version. The fix must serve iteration
without sacrificing immutability.

## Decisions in detail

### D1 — Registry is the only run-resolution source
Runs, `mdk run --target`, and `GET /api/v1/agents` resolve **exclusively** from the
registry (`get_agent_bundle`). The filesystem agent dir is, at most, a staging
artifact; it never decides what executes. The create/PUT-to-dir behavior is
removed as a source of truth (kept only if some non-run path still needs a
materialized dir, in which case it is rederived from the registry record).

### D2 — Content-addressed auto-versioning on deploy
On `mdk deploy`, compute a `content_hash` over the bundle (agent.yaml + prompt +
schemas + declared skills/contexts — the same hash already stored on
`AgentBundleRecord`). Then:

- **`content_hash` == latest published version's hash** for `(name, tenant)` →
  **no-op**. Report `no change — <name> <version> already published`. Never print
  `✓ uploaded`.
- **`content_hash` differs** → **publish a new `AgentBundleRecord`**.
  - If `agent.yaml: version` was bumped to an unpublished value → publish that
    version.
  - If `version` is unchanged but content differs → **auto-derive a distinct,
    monotonic version** so the immutable `(name, version)` constraint holds *and*
    "latest" becomes the new content. Preferred encoding: PEP 440 build metadata
    `<version>+<hash8>` (e.g. `0.1.0+9f3a1c0d`), which sorts deterministically and
    makes the lineage auditable; the human `version` label stays as authored.

The operator gets the iterate loop "for free" (edit → deploy → new content runs)
with **zero loss of immutability** — every distinct content is its own immutable,
content-addressed version, and history is preserved.

### D3 — Honest, transparent deploy output
Deploy reports the published identity per agent: `published <name> 0.1.0+9f3a1c0d
(content changed)` or `no change — <name> 0.1.0+… already published`. No
`✓ uploaded` for a no-op (the misleading signal that masked this bug). The
post-deploy next-steps already point at `mdk run`/`mdk runs show`; they now also
name the version that will run.

### D4 — Pinning, rollback, and the `?version` getter
- `mdk run <name> --target <env>` resolves **latest**; `--version <v>` (and the
  API `?version=`) resolve an **exact** version — and the getter MUST honor the
  param (current bug). Rollback = re-point/-publish a prior version as latest
  (or run it explicitly by version).
- Version *history* is intact: every publish is a row; `mdk runs show` / the
  registry list expose the lineage.

### D5 — Scope split: immediate fix (#93) vs. durable model (this ADR)
The minimum viable fix (task #93) is **"re-deploy persists the changed bundle to
the registry and runs serve latest"** — even a plain overwrite-latest would
unblock the loop. This ADR sets the *durable* model (content-addressed
auto-versioning + immutability) so the quick fix lands in a direction we won't
have to reverse. Implement #93 against D1+D2.

## Consequences

**Positive**
- The core MVP loop — *edit → redeploy → see the new behavior* — works, with no
  manual version bumping.
- Immutability, audit, reproducibility, and rollback (ADR 014) are **preserved**:
  every distinct content is its own immutable version; a run still pins exact
  content.
- No-op redeploys become honest (`no change`), killing the misleading
  `✓ uploaded`.
- Lays groundwork for content-addressed promotion across environments
  (dev → staging → prod ship the same `content_hash`).

**Negative / risks**
- **Version-table growth**: every content change is a new row. Mitigated by the
  `content_hash` dedup (identical content = no new row) and bounded by human
  iteration cadence; add retention/GC for `+hash` build-versions if it ever
  matters.
- **Blast radius**: touches the deploy upload path (`deploy.py`), the runtime
  create/PUT endpoint (`agent_creation.py`), and run/list resolution. Pinned-
  version consumers must keep resolving exact versions — covered by D4 but needs
  test coverage.
- **Version-string ergonomics**: `0.1.0+<hash>` is unusual to operators; surfaced
  clearly in deploy output and `mdk runs show` so it's legible, not magic.

**Net-new / changed:** `_upload_one_agent_bundle` (publish-to-registry on content
change, drop dir-as-truth), `agent_creation.py` (PUT upserts the registry or is
retired as a run source), `get_agent_bundle` (honor `?version`), the
version-derivation helper, and deploy output wording.

## Alternatives considered

- **(a) Overwrite-latest (mutable rows).** Re-deploy overwrites the existing
  `(name, version)` row's content. *Rejected:* breaks ADR 014 immutability — a
  past run's version no longer pins the exact content it executed, killing
  reproducibility and clean rollback.
- **(b) Require a manual version bump + a clear error.** Deploy errors `version X
  already published with different content — bump version: to publish`. Simple, no
  model change. *Rejected as the end-state* (kept only as an interim guardrail):
  it pushes friction onto the single most common action — iterating — and a fresh
  operator hits a wall on their first edit.
- **(c) Pure content-addressing (drop the `version` field as identity).** Identity
  = `content_hash`; `version` is a pure label. Conceptually cleanest, but a larger
  schema/UX change. *Deferred:* D2 ("auto-derive version, dedup by hash") captures
  most of the benefit with far less churn, and can evolve toward (c) later.
