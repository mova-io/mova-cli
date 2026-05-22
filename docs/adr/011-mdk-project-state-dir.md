# ADR 011 — Rename the project state dir `.movate/` → `.mdk/`

**Status:** Proposed
**Date:** 2026-05-22
**Deciders:** Engineering
**Context window:** Authoring-ergonomics polish
**Supersedes:** N/A
**Related:** `src/movate/cli/init.py` (creates + documents the dir),
`src/movate/snapshot/store.py`, `src/movate/promotions/store.py`,
`src/movate/cli/{doctor,add_cmd,ci,fmt_cmd}.py`, `src/movate/menu/status.py`,
`.gitignore`

---

## Decision

Rename the **project-level** runtime-state directory from `.movate/` to
**`.mdk/`**, via a **read-compatible resolver** (no forced data move) plus an
**opt-in migration command** — so existing projects keep working untouched and
new projects get the clearer name.

1. **New canonical name: `.mdk/`** — matches the CLI binary (`mdk`), clearly
   "the tool's local dir" (like `.git` / `.vscode`).
2. **One resolver, one source of truth.** Introduce
   `project_state_dir(root) -> Path` and route every current `.movate/`-literal
   call site through it. Resolution: prefer `.mdk/` if present, else legacy
   `.movate/` if present, else default to `.mdk/` (fresh projects).
3. **No automatic data move.** The resolver only *reads* both names; it never
   silently `mv`s a user's dir (their eval baselines + snapshot history are
   git-tracked — moving them would dirty the tree and rewrite history).
4. **Opt-in migration**: a `mdk migrate-state` command (`git mv .movate .mdk` +
   rewrite `.gitignore`) for projects that want to switch, plus a one-shot,
   opt-out hint when a legacy `.movate/` is detected.
5. **Scope: project-level only.** The machine-global `~/.movate/`
   (credentials / config / profiles / secrets) is **out of scope** and
   unchanged.

In one sentence: **"new projects get `.mdk/`; old projects keep `.movate/`
working via a read-compat resolver and migrate on demand — only the project
state dir renames, not the global config."**

---

## Context

When you initialize a project, `mdk` creates a runtime-state directory
(`init.py:228`) holding: `local.db` (run/failure SQLite), `snapshots/<hash>/`
(content-addressed project snapshots powering diff/rollback/audit/promote),
eval baselines (`baseline.json`, *committed* to git), `promotions.yaml`, and
LLM-scaffold failure dumps. Today it's named `.movate/`.

Two problems with the name:

* **It reads as snapshots-only / brand-noise.** Operators see
  `.movate/snapshots/` and assume `.movate/` *is* snapshots; it actually holds
  five different things.
* **It collides conceptually with the global `~/.movate/`** (credentials/config).
  Two different `.movate/`s — one per-project state, one per-machine config —
  is confusing.

`.mdk/` (the CLI's own name) fixes both: it's unambiguously "the `mdk` tool's
local working dir," parallel to how `.git/` is git's.

The constraint: the name is hardcoded in ~10 source files **and** existing
projects have real, partly git-tracked content under `.movate/`. A naive rename
is a breaking change to a project-layout contract. This ADR is about renaming
*without* breaking those projects.

---

## Decision drivers

| Driver | Weight |
|---|---|
| **Clarity** — name should say "mdk's local dir," not imply "snapshots" or collide with the global config | HIGH |
| **Backward compatibility** — existing projects (committed baselines, snapshot history, CI eval gates reading `.movate/*/baseline.json`) must keep working | HIGH |
| **Single source of truth** — the dir name is hardcoded in ~10 places; centralize so this never drifts again | HIGH |
| **No surprise data moves** — never silently `mv` a user's git-tracked dir | HIGH |
| **Brand consistency** — `.mdk/` matches the `mdk` CLI; `~/.movate/` stays the home-config convention | MED |

---

## Architecture

```
every call site (snapshot/, promotions/, doctor, add, ci, fmt, menu, init)
        │  was: root / ".movate" / …
        ▼
  movate.core.paths.project_state_dir(root)        ── single resolver
        │
        ├─ .mdk/      exists → use it
        ├─ .movate/   exists → use it   (legacy, read-compat)
        └─ neither    → .mdk/           (fresh projects)
```

`is_project_root()` recognizes a project by `project.yaml` (etc.), independent
of the state dir, so detection is unaffected. The resolver is the *only* place
the literal directory names live after this change.

---

## Decisions

### Decision 1 (D1): `.mdk/`, not `.snapshots/` or keeping `.movate/`

`.mdk/` matches the CLI and reads as "the tool's dir." Rejected alternatives:
- **`.snapshots/`** — a misnomer; snapshots are 1 of 5 tenants (also `local.db`,
  baselines, promotions, init dumps). It would mislead more than `.movate/` does.
- **Keep `.movate/`** — leaves the global-config name collision and the
  "snapshots?" confusion unsolved.

### Decision 2 (D2): Read-compat resolver, never an automatic move

The resolver prefers `.mdk/`, falls back to legacy `.movate/`, and defaults new
projects to `.mdk/`. It **does not** move data: eval baselines
(`.movate/*/baseline.json`) are committed to git and read by CI eval gates;
snapshot directory names are content-addressed history. Silently `mv`-ing them
would dirty the working tree, require an unexpected commit, and could desync CI.
So migration is explicit (D4), not a side effect of upgrading the CLI.

### Decision 3 (D3): One resolver replaces ~10 hardcoded literals

`.movate/` is currently spelled out in `snapshot/store.py`,
`promotions/store.py`, `doctor.py`, `add_cmd.py`, `ci.py`, `fmt_cmd.py`,
`menu/status.py`, and `init.py`. Centralizing into
`movate.core.paths.project_state_dir()` (core is importable by all of those
without a boundary inversion) is the prerequisite refactor and prevents the
name from drifting again.

### Decision 4 (D4): Explicit `mdk migrate-state` + one-shot hint

Provide `mdk migrate-state` to convert a project on demand: `git mv .movate
.mdk` and rewrite the `.gitignore` block. When the resolver falls back to a
legacy `.movate/`, emit a **one-shot, opt-out** hint (`MDK_NO_STATE_HINT=1`)
pointing at the command — mirroring the `movate.yaml → project.yaml` deprecation
UX. Quiet by default after the first nudge.

### Decision 5 (D5): Project-level only; `~/.movate/` is untouched

This ADR renames *only* the per-project state dir. The machine-global
`~/.movate/` (credentials, config, profiles, secrets) keeps its name — it's the
standard home-config location, isn't the source of the "snapshots?" confusion,
and renaming it would be a far larger break (every operator's credentials). A
future ADR may revisit global-config naming; not here.

### Decision 6 (D6): `.gitignore` covers both during the transition

The shipped `.gitignore` (and `init`-scaffolded ones) ignore **both** `.mdk/`
and `.movate/`, preserving the existing baseline exception
(`!<dir>/*/baseline.json`) for each, so neither old nor migrated projects leak
runtime state or lose tracked baselines.

---

## Consequences

**Positive**
- Clearer mental model: `.mdk/` = "mdk's local dir"; no collision with `~/.movate/`.
- Zero breakage for existing projects (read-compat); migration is opt-in.
- The dir name has a single home, so it can't drift across the codebase again.

**Negative / costs**
- A transition period where both names exist in the wild; docs + `.gitignore`
  must mention both. The one-shot hint adds a (small, opt-out) line for legacy
  projects.
- A project containing **both** dirs resolves to `.mdk/` (documented;
  `migrate-state` refuses to clobber a non-empty `.mdk/`).

**Neutral**
- New env var `MDK_NO_STATE_HINT` to silence the migration nudge.

---

## Implementation plan (separate PRs, after this ADR is accepted)

1. `movate.core.paths.project_state_dir()` resolver + constants; refactor the
   ~10 call sites to use it (no behavior change yet — resolver still finds
   `.movate/` on existing projects). Tests for the precedence order.
2. New-project scaffolding (`init`) writes `.mdk/`; `.gitignore` template + the
   repo's own `.gitignore` cover both names; refresh the dir's `README`.
3. `mdk migrate-state` command + the one-shot legacy hint.
4. Docs sweep (`init.py` block, any `.movate/` references in `docs/`).
