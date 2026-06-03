# ADR 059 — Assign CalVer at merge, not in the PR (kill the version-line merge tax)

**Status:** Superseded (2026-05-31) — the at-merge `release-version.yml` it
introduced pushed the bump commit **directly to `main`**, which the org's branch
protection wouldn't authorize for any available token (`403 — denied to
<user>`); it failed on every merge, freezing the version at the last good bump.
Reverted to **per-PR bump** (run `scripts/bump_version.py` in the PR; the merge
queue lands it — no push-to-`main` token needed). The at-merge workflow was
removed. See CLAUDE.md "Versioning".
**Date:** 2026-05-30
**Deciders:** Engineering (process/release)
**Context window:** remove the dominant source of merge friction — the version
living in the branch — by assigning the CalVer version **once, at merge**,
instead of bumping it in every PR.
**Builds on / composes with:**
the CalVer system (`scripts/bump_version.py`, format `YYYY.M.D.N` — unchanged),
`.github/workflows/ci.yml` (the **"Version bump gate"** step this ADR removes),
ADR 058 (the self-syncing roadmap reads `shipped_version` = main's version at
merge — which now comes from the post-merge bump), and the **GitHub merge
queue** (a repo setting that composes with this — see D4).

**Defining evidence.** In a single high-throughput session, *every* unit of
merge pain traced to one root cause: the version is committed **in the branch**.
That produced — repeatedly — (a) version-file merge conflicts when two PRs both
edit `pyproject.toml`/`__init__.py`/`uv.lock`, (b) `--gate-ahead-of` failures
when `main` advanced to the same version a PR carried (`.6` vs `.6`), (c) serial
re-bumps where each merge forced a re-bump on every other open PR, and (d) a
whole class of breakage from the `.githooks` auto-bump not propagating to
worktrees. **Take the version out of the branch and all four disappear at once.**

This is a **process/release** ADR: `ci.yml` + one new workflow + a convention +
docs. **No runtime/`core`/storage/API change; the CalVer *format* is unchanged.**

---

## Context

A per-PR version bump makes the version a *contended, branch-local* value: two
PRs off the same base both change the same three lines, so they conflict; and a
gate that demands "strictly ahead of base" turns every merge into a forced
re-bump of every sibling. None of this protects anything real — the version is
**derivable** (date + a counter), so it should be **assigned by the trunk at
merge time**, not negotiated between branches. The branch should carry *code*;
the trunk should carry the *version*.

## Decision

PRs no longer bump the version; CalVer is assigned once, at merge, on `main`.

### D1 — PRs do not touch version files

Branches leave `pyproject.toml` / `src/movate/__init__.py` / `uv.lock` at
whatever `main` has. No version diff in a PR ⇒ **no version-file conflicts**, and
nothing for siblings to re-bump. (Agents/contributors stop running
`bump_version.py` in branches — docs + the removed gate enforce this.)

### D2 — Remove the per-PR "Version bump gate"

Delete the `ci.yml` "Version bump gate" step (`bump_version.py --gate-ahead-of
$BASE_SHA`). A PR is no longer required to be version-ahead of base — because it
no longer carries a version at all. (The **pyproject↔uv.lock consistency check**
stays; that catches a real dep-drift bug and is orthogonal.)

### D3 — Assign CalVer at merge (post-merge workflow)

A new `.github/workflows/release-version.yml` triggers on `push: main`
(paths-ignore the version files to avoid self-trigger): it runs
`bump_version.py`, computes the next `YYYY.M.D.N`, and commits
`chore(release): 2026.M.D.N [skip ci]` back to `main`. So `main`'s version
always reflects "the latest merge," assigned by the trunk, exactly once per
merge — which is precisely what ADR 058's roadmap stamps as `shipped_version`.

### D4 — Compose with the GitHub merge queue (repo setting)

Enable the **GitHub merge queue** (branch-protection setting — operator action).
With D1–D3, the queue's "require up-to-date" becomes cheap (no version conflict
to resolve on update) and merges serialize *without human re-bumping*. The
post-merge bump (D3) can alternatively run *inside* the queue; either is fine.
This ADR makes the merge queue finally painless; the two together end the tax.

### D5 — Transition

In-flight PRs that already carry a bump are harmless (their version diff just
sets `main` to that value on merge; the post-merge bump moves it forward). New
PRs stop bumping immediately once D2 lands. No history rewrite, no flag day.

## Consequences

**Positive**
- **Zero version-file conflicts**; **zero forced re-bumps**; the `.githooks`
  propagation problem becomes moot. The single biggest merge tax is gone.
- The version is assigned by the **trunk**, the one place that can do it
  unambiguously — and lands exactly where ADR 058 wants it.
- Faster, parallel-friendly PR flow; the merge queue becomes painless (D4).

**Negative / risks**
- **A bot bump-commit per merge** — mitigated by `[skip ci]` + paths-ignore so it
  never loops or re-runs CI. (Batchable to one-per-merge-group under the queue.)
- **Concurrent merges race the bump** — each merge produces its own bump commit;
  worst case is two adjacent version numbers, never a conflict (the counter is
  monotonic by date+N). Acceptable.
- **A stray local bump** — harmless (D5); CI no longer depends on it.

## Boundaries

CI/release tooling only: remove one `ci.yml` step, add one workflow, update
`scripts/bump_version.py` invocation site + docs (CLAUDE.md "Versioning" note).
The CalVer **format and the `bump_version.py` computation are unchanged** — only
*where/when* it runs moves (branch → trunk-at-merge). No product behavior change.

## Alternatives considered

- **Keep version-in-PR, just enable the merge queue.** Rejected as sufficient —
  the queue serializes merges but PRs still *conflict on the version files* and
  still need re-bumps; the queue can't fix a branch-local contended value. D1 is
  the actual fix; the queue (D4) is the complement.
- **Manual/tagged versioning (bump only on release tags).** Rejected — loses the
  per-merge CalVer that ADR 058's roadmap + `mdk --version` traceability rely on.
- **Keep the gate but auto-rebase+rebump via a bot.** Rejected — automates the
  symptom (re-bumping) instead of removing the cause (version in the branch).

## Scope / rollout

1. **Remove the gate (D2) + add the post-merge bump workflow (D3) + stop
   PR-side bumps (D1) + docs** (CLAUDE.md "Versioning" + a CONTRIBUTING note).
2. **Enable the merge queue (D4)** — operator repo-setting.
3. Optional: move the bump *into* the merge-group step for one-bump-per-group.
