# ADR 066 — Derive CalVer at build time from git; stop committing the version

**Status:** Accepted (2026-06-06)
**Date:** 2026-05-31
**Deciders:** Engineering (process/release)
**Context window:** end the version-bump merge tax *permanently* by removing the
version from committed files entirely — the trunk no longer carries a version
*line*, it carries the *history*, and CalVer is **computed from that history at
build time**. Closes the loop left open by ADR 059.
**Builds on / composes with:**
the CalVer scheme (`scripts/bump_version.py`, format `YYYY.M.D.N` — the *format*
is unchanged), `hatchling` (already our `build-backend` — this ADR adds a
version *source*), ADR 058 (the self-syncing roadmap reads `shipped_version` =
main's version — now computed, not read from a committed line), ADR 059
(at-merge bump — **this ADR supersedes its mechanism**), and the GitHub merge
queue.

**Defining evidence.** We have now hit **both** failure modes of a
*committed* version, in the same week:
- **Per-PR bump (current):** the version lives in three branch-local files
  (`pyproject.toml` / `src/movate/__init__.py` / `uv.lock`), so sibling PRs
  **conflict on the version line**, every merge forces a **re-bump of every open
  PR**, and the `.githooks/pre-commit` auto-bump **doesn't propagate to
  worktrees**. With 25 open PRs this is a continuous tax.
- **At-merge bump (ADR 059):** moving the bump to a post-merge workflow required
  the bot to **push the bump commit to `main`**, which the org's branch
  protection **won't authorize for any available token** (`403 — denied`). It
  failed on *every* merge and **froze the version** (2026.5.30.x stuck on
  2026-05-31).

Both failures share one root cause: **the version is a committed artifact.**
A per-merge counter that's *derivable from date + history* should never be
negotiated between branches *or* written back by a bot. Take it out of the
committed tree and **both classes of failure disappear at once** — no line to
conflict on, and no push-to-`main` to authorize.

This is a **process/release** ADR: a build-backend version *source* + removing a
script/hook from the flow + docs. **No runtime/`core`/storage/API behavior
change; the CalVer *format* (`YYYY.M.D.N`) is unchanged; `mdk --version`,
`movate.__version__`, and the API version field are preserved.**

---

## Context

ADR 059 correctly diagnosed that *"the version is committed in the branch"* is
the single biggest merge tax, and that the version is **derivable** (date + a
counter) so the **trunk** should own it. But 059's *mechanism* — a post-merge
workflow that **commits the bump back to `main`** — collided with branch
protection: the trunk can't grant a bot push-to-`main` rights in this org. The
diagnosis was right; the mechanism was wrong.

The fix that honors the diagnosis *without* writing to the trunk is to never
materialize the version as a committed file. `hatchling` (already our build
backend) supports a **version source** computed at build/package time. CalVer
becomes a pure function of git history — assigned by whoever builds the
artifact, identical for the same checkout, and **absent from every branch's
working tree**. There is nothing to conflict on and nothing to push.

## Decision

The version is **computed from git at build time**, not stored in any committed
file. PRs never touch a version; no bot ever writes one back.

### D1 — A build-time CalVer version source (no committed version line)

Add a small hatch version source (`scripts/calver_version.py`, registered via
`[tool.hatch.version] source = "..."`) that computes `YYYY.M.D.N` from the repo:
- **`YYYY.M.D`** = the authored date (UTC) of `HEAD`.
- **`N`** = the count of commits sharing that calendar day (the monotonic
  per-day counter — same semantics `bump_version.py` produced today).
- **dev/uncommitted builds** append a PEP 440 local segment `+g<shortsha>[.dirty]`
  (never published; release artifacts built from a clean checkout omit it).

`version = "..."` is **deleted** from `pyproject.toml` (replaced by
`[tool.hatch.version]`), and the static `__version__` line is removed from
`src/movate/__init__.py`.

### D2 — Runtime reads the version from package metadata

`movate.__version__` resolves via `importlib.metadata.version("movate")` (with a
git-derived fallback for an editable checkout whose metadata is stale). So
`mdk --version`, the `/api/v1` version field, and ADR 058's `shipped_version`
all keep working — they read the *installed* version, which hatch baked in at
build. **No call site changes its contract.**

### D3 — sdists/wheels carry a frozen version; the repo never does

At build, hatch writes a generated `_version.py` (gitignored) and bakes the
computed CalVer into the sdist/wheel metadata. So **built artifacts are always
pinned and reproducible**, while the **working tree carries no version at all**.
This is the crux: pinned where it must be (artifacts), absent where it hurts
(branches).

### D4 — Remove the per-PR bump and the auto-bump hook from the flow

`scripts/bump_version.py` stops being part of the PR/commit flow; the
`.githooks/pre-commit` bump step is removed (it caused the worktree-propagation
breakage). The script may be retained only as a thin wrapper that *prints* the
computed version for CI/debugging — it no longer **edits** files. The `ci.lock`
/ `pyproject ↔ uv.lock` consistency check stays (orthogonal, still valuable).

### D5 — Transition (no flag day)

The moment D1–D4 land, new PRs stop carrying a version diff. In-flight PRs that
still bump a version line are harmless — the line is deleted on rebase, and
nothing depends on it once the version source is git-derived. No history
rewrite. ADR 059's `release-version.yml` is already removed (it was failing);
this ADR makes its absence permanent and *correct*, not a regression.

## Consequences

**Positive**
- **Zero version-file conflicts, zero forced re-bumps, no githook propagation
  problem** — the same wins ADR 059 sought, now achieved *without* a push to
  `main`. The 25-PR backlog stops re-bumping itself.
- **No privileged token, no bot write to the trunk** — sidesteps the exact
  `403` that froze the version. Works under the org's existing protection.
- **Reproducible, traceable builds** — the same checkout always yields the same
  CalVer; artifacts are pinned; the repo stays clean.

**Negative / risks**
- **CI must fetch full history** (`actions/checkout` with `fetch-depth: 0`) or
  `N` undercounts on a shallow clone. *Mitigation:* set it in the build/release
  jobs; the version source falls back to commit-distance + `+g<sha>` if history
  is shallow, so a build never *fails*, it just degrades the counter.
- **Editable installs can show a stale metadata version** until reinstalled.
  *Mitigation:* the D2 git-derived fallback for non-clean/editable trees.
- **A new build-time code path** (the version source) — small, pure, unit-tested
  against fixed git fixtures; no network, no `Date.now`-style nondeterminism
  (it reads commit dates, not wall-clock).

## Boundaries

CI/release/build tooling only: one hatch version source, a `pyproject.toml`
`[tool.hatch.version]` stanza, removal of a static line in two files, removal of
the pre-commit bump step, and docs (CLAUDE.md "Versioning"). The CalVer
**format and counter semantics are unchanged**; only *where the version lives*
moves (committed file → computed-from-git). **No product/runtime/API/storage
behavior change.**

## Alternatives considered

- **Keep per-PR bump (status quo).** Rejected — it is the documented dominant
  merge tax; the defining evidence above is from this week.
- **ADR 059's at-merge post-merge bump.** Rejected — requires push-to-`main`
  the org won't authorize (`403`); already proven to fail.
- **Tag-driven `hatch-vcs` (version from git tags).** Rejected as the *primary*
  mechanism — it moves the write to a **tag per merge** (still a per-merge write
  + a privileged action), and CalVer's date+counter doesn't map cleanly to
  tag-distance. (We *could* still tag releases for humans; the version source
  doesn't require it.)
- **Bump inside the merge-group step.** Rejected — still writes a version commit
  into the merge, reintroducing a committed artifact and merge-group flakiness;
  D1 removes the artifact entirely, which is strictly simpler.

## Scope / rollout

1. Add `scripts/calver_version.py` (hatch version source) + `[tool.hatch.version]`
   in `pyproject.toml`; delete the static `version =` line.
2. Remove `__version__` literal from `src/movate/__init__.py`; resolve via
   `importlib.metadata` (D2) with a git fallback.
3. Remove the `.githooks/pre-commit` bump step; demote `bump_version.py` to a
   read-only "print the computed version" helper (or delete).
4. Set `fetch-depth: 0` on build/release CI jobs.
5. Update CLAUDE.md "Versioning" + a CONTRIBUTING note: *contributors never bump
   a version; it is computed from git.*
6. Unit-test the version source against fixed git fixtures (date rollover,
   multiple commits/day, shallow clone, dirty tree).
