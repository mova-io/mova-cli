# ADR 058 — Self-syncing roadmap keyed on CalVer (generated + freshness-gated)

**Status:** Proposed
**Date:** 2026-05-30
**Deciders:** Engineering (process/tooling)
**Context window:** stop the roadmap from drifting from reality by making
**CalVer the join key** between roadmap items, the PRs that delivered them, and
the version they shipped at — and making the rendered roadmap a **generated,
CI-freshness-gated artifact** rather than a hand-maintained doc.
**Builds on / composes with:**
the CalVer versioning system (`scripts/bump_version.py`, `.githooks/pre-commit`
— one bump per PR, so the version is a dense per-PR identifier),
`docs/implementation-tracker.md` (which already stamps a **"Merged" CalVer
column per item** — the right idea, done by hand and therefore stale),
`BACKLOG.md`, the existing **daily-changelog GitHub Action** (already maps
merged PRs → a tracking issue; we extend it), and the
**freshness-check pattern** shipped this session for `docs/openapi.json` /
`docs/cli-reference.md` (a `--check` mode that fails CI when a committed
generated artifact is stale — reused here).

**Defining problem.** We maintain three disconnected trackers: the **harness
task list** (excellent in-session, but ephemeral — it vanishes between
sessions), the **committed roadmap** (`implementation-tracker.md` / `BACKLOG.md`
— durable but hand-edited, so it goes stale the moment a busy session ships 14
PRs without updating it), and **CalVer** (the only one that is *automatic and
truthful* — every merge bumps `YYYY.M.D.N`). Nothing joins them, so the roadmap
lies. This ADR makes the roadmap **derive its "shipped" truth from CalVer +
merge metadata** and **gates that derivation in CI**, so it cannot drift.

This is a **process/tooling** ADR: docs + `scripts/` + one GitHub workflow +
one CLI command. **No runtime / `core` / storage / API change**; nothing about
the product behavior moves.

---

## Context

This session shipped ~14 PRs in hours; the hand-maintained tracker was stale by
PR #3. The pain is structural, not discipline: a human (or agent) updating a
markdown table by hand, per PR, mid-flight, will always fall behind. Meanwhile
the information needed to keep it current already exists and is automatic — the
**CalVer bump on every merge** plus the **PR that caused it**. The fix is to
treat the roadmap like we already treat the OpenAPI spec and the CLI reference:
a **generated artifact with a CI freshness gate**, sourced from machine-readable
truth, never hand-edited in its rendered form.

## Decision

Make the roadmap a generated, CalVer-keyed artifact with a CI freshness gate and
an auto-stamp-on-merge action.

### D1 — `roadmap.yaml` is the single durable source of truth

A machine-readable `roadmap.yaml` at the repo root holds every item:

```yaml
items:
  - id: temporal-exec-backend       # stable, human-meaningful id
    title: Temporal as an executable backend (dispatch fork)
    status: shipped                 # planned | in_progress | shipped | dropped
    pr: 618
    shipped_version: 2026.5.30.6    # the CalVer at merge (D2) — auto-stamped
    adr: 055
    depends_on: [temporal-track-b, temporal-track-c]
  - id: durable-hitl
    title: Durable HITL on Temporal (HUMAN node)
    status: planned
    adr: 054
    depends_on: [temporal-exec-backend]
```

The **`planned`/`in_progress` items are hand-authored** (intent lives with
humans, CLAUDE.md). The **`pr`/`shipped_version`/`status: shipped` fields are
auto-stamped** (D4) — humans never hand-write "done @ version" again.

### D2 — CalVer is the join key; `shipped.jsonl` is the append-only ledger

Because every merge bumps CalVer exactly once, the version is a dense per-PR
identifier. An append-only `shipped.jsonl` records the ground truth, one line
per merge:

```json
{"version":"2026.5.30.6","pr":618,"id":"temporal-exec-backend","title":"…","merged_at":"…"}
```

This ledger is the durable, auditable map of *what shipped at what version* —
reconstructable from git history alone if ever lost (the version bump is in each
merge commit). `roadmap.yaml`'s shipped fields are projected from it.

### D3 — PR ↔ roadmap convention (lint-warned, not blocked)

Every `feat`/`fix` PR carries a `Roadmap-Id: <id>` trailer in its body (or a
matching GH label). A lightweight CI check **warns** (does not fail) when a
feat/fix PR lacks one, so the join is encouraged without blocking emergency
fixes. A PR may create a *new* `planned`-less item by declaring an id not yet in
`roadmap.yaml` — the stamp action (D4) appends it.

### D4 — Auto-stamp on merge (extend the daily-changelog Action)

Extend the existing **daily-changelog GitHub Action** (or a sibling on
`push: main`): for each new merge commit, read (a) the **CalVer** from the bumped
`pyproject.toml` at that commit and (b) the merged PR's `Roadmap-Id`, then:
1. append a line to `shipped.jsonl`,
2. set the matching `roadmap.yaml` item to `status: shipped` + `pr` +
   `shipped_version`,
3. regenerate `ROADMAP.md` (D5) and commit the three files back to `main`
   (bot commit, `[skip ci]`-tagged on the doc-only stamp to avoid a CI loop —
   the bump-gate already ran on the PR).

No human touches "done" state; it follows the merge automatically.

### D5 — `ROADMAP.md` is generated + freshness-gated in CI

`scripts/gen_roadmap.py` renders `ROADMAP.md` from `roadmap.yaml` + `shipped.jsonl`:
a **Shipped** table (item · PR · `shipped_version`, newest CalVer first), an
**In progress / Planned** section, and a **Blocked-by** view from `depends_on`.
`scripts/gen_roadmap.py --check` (run in CI, exactly like the
`export_openapi.py --check` / `gen_cli_reference.py --check` gates shipped this
session) **fails the build if the committed `ROADMAP.md` is stale**. Result: the
rendered roadmap *cannot* diverge from `roadmap.yaml`/`shipped.jsonl`, and those
*cannot* diverge from CalVer reality (D4). The roadmap is now structurally
incapable of lying.

### D6 — `mdk roadmap` to view it

A read-only `mdk roadmap` command (`--json` too) surfaces: what shipped at which
version, what's in progress/planned, and what's blocked — sourced from
`roadmap.yaml`/`shipped.jsonl`. Makes the synced roadmap a first-class operator/
contributor view, not just a doc.

### D7 — The harness task list stays ephemeral scratch; `roadmap.yaml` is durable

The in-session task list (TaskCreate) is excellent working memory but is *not*
the source of truth and is *not* persisted here. The reconciliation rule: when
work is committed, its item lives in `roadmap.yaml` (created `planned`/
`in_progress` by hand or the first PR's `Roadmap-Id`); the stamp action (D4)
moves it to `shipped`. A session may end with scratch tasks that never became
roadmap items — that's fine; only committed work earns a roadmap row. (A future
nicety: a `/session-end` reconciliation that offers to promote open tasks into
`roadmap.yaml` — out of scope here.)

## Consequences

**Positive**
- The roadmap **cannot drift** — it's generated from CalVer-joined truth and
  CI-gated for freshness (the proven OpenAPI/parity-gate pattern).
- "What shipped at what version" becomes **queryable and auditable**
  (`shipped.jsonl` / `mdk roadmap`), reconstructable from git if lost.
- Zero hand-maintenance of "done" state; humans only author *intent* (planned
  items + dependencies).

**Negative / risks**
- **A bot commit on every merge** (the stamp) — mitigated by `[skip ci]` on the
  doc-only stamp + batching (the daily-changelog cadence is an option if
  per-merge commits are noisy).
- **Convention adherence** — a PR without a `Roadmap-Id` doesn't auto-stamp;
  mitigated by the D3 lint-warning + the fallback that the ledger can be
  back-filled from git history.
- **Two roadmap docs during transition** — `implementation-tracker.md` /
  `BACKLOG.md` migrate into `roadmap.yaml`; deprecate-before-remove (rule 5),
  redirect them to the generated `ROADMAP.md`.

## Boundaries

Pure process/tooling: `roadmap.yaml` + `shipped.jsonl` + `scripts/gen_roadmap.py`
+ one GitHub workflow change + a read-only `mdk roadmap`. **No `core`/`runtime`/
`storage`/`/api/v1` change.** Reuses the existing CalVer system (source of the
join key), the daily-changelog Action (the stamp host), and the freshness-check
pattern (the anti-drift gate). Adapt — don't adopt: no external PM tool.

## Alternatives considered

- **Keep hand-maintaining the tracker.** Rejected — this session is the proof it
  doesn't survive throughput.
- **Pure git-derived roadmap, no `roadmap.yaml`/IDs.** Rejected as the *whole*
  solution — git can derive *shipped* (kept, as the `shipped.jsonl`
  back-fill/reconstruct path) but can't express *planned* items, dependencies,
  or intent. Keep both: YAML for intent, CalVer/git for truth.
- **External PM tool (Jira/Linear/GH Projects) as source of truth.** Rejected —
  not in-repo, not CalVer-joined, not CI-gateable; the roadmap should live with
  the code and be enforced by the same CI that guards everything else. (GH
  Projects can still *consume* `shipped.jsonl` via the Action.)
- **Make the harness task list durable.** Rejected — it's session-scoped working
  memory by design; the durable record belongs in the repo.

## Scope / rollout

Multi-PR; this ADR is doc-only.

1. **`roadmap.yaml` + `scripts/gen_roadmap.py` (+ `--check`) + generated
   `ROADMAP.md` + the CI freshness gate**, and **back-fill this session's ~14
   merged PRs** into `roadmap.yaml`/`shipped.jsonl` (the immediate payoff: the
   whole session mapped to CalVer). No behavior depends on the Action yet —
   freshness is enforced from day one.
2. **The auto-stamp Action** (extend daily-changelog) + the `Roadmap-Id`
   convention + the D3 lint-warning.
3. **`mdk roadmap`** command (D6).
4. Migrate + deprecate `implementation-tracker.md` / `BACKLOG.md` into the
   generated roadmap.
