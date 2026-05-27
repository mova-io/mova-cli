# ADR 026 — `mdk init` front-door UX: always a runnable project, name resolution, editor launch

**Status:** Accepted
**Date:** 2026-05-27 (proposed); 2026-05-27 (approved, status flipped to Accepted)
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x onboarding — make the very first command a user runs
(`mdk init …`, especially `--llm`) leave them in a **runnable, navigable
project** that `run`/`validate`/`dev` understand by name, with the editor open —
instead of a bare agent folder that the downstream commands can't resolve.
**Related / constrained by:** ADR 002 (project layout: `agents/<name>/` +
`project.yaml`), ADR 025 (`AGENTS.md` scaffold + authoring copilot — a project is
where it lives), ADR 021 (snapshot baseline on project create), the canonical
agent layout (#127), the `--llm` RAG slice (F2/F3/F5–F8), and `_resolve_project_root`
/ `load_agent` (`core/loader.py`).

## Decision

**`mdk init <name>` always produces (or extends) a runnable project.** Whether
bare, `-t <template>`, or `--llm "<desc>"`:

- **Outside a project** → scaffold a project: `project.yaml` + `.env.example` +
  `.gitignore` + `AGENTS.md` (ADR 025) + the agent under `agents/<name>/` + an
  initial snapshot (ADR 021).
- **Inside a project** → add the agent under `agents/<name>/` of the current
  project (like `mdk add`).
- `--bare` keeps a **standalone agent** (no `project.yaml`/`agents/`) for dropping
  an agent into a non-mdk repo or a quick single-agent experiment. A standalone
  agent dir is a **first-class runnable unit** (D2) — NOT a degraded mode: the
  loader already loads any dir with an `agent.yaml`, so `run`/`validate`/`dev`
  fully support it.

Plus five supporting fixes so the result "just works":
**(D2)** `run`/`validate`/`dev` resolve an agent **by name** (→ `agents/<name>/`)
in addition to a path, AND treat a standalone agent dir as first-class (`mdk run .`
/ cwd auto-detect), with friendly errors. **(D3)** a shared `_launch_editor()`
opens the new folder after `--llm`/project init (matching today's project-mode
launch). **(D4)** next-steps print the **exact** runnable command. **(D5)**
`mdk doctor` flags a **stale install**. **(D6)** the LLM that powers `--llm` is
**configurable** via a layered precedence (flag > env > project > user-config >
key-matched default).

## Context

Today (confirmed in `cli/init.py`): **agent mode is the default** — `mdk init
<name>` scaffolds a bare agent dir at `<target>/<name>/`; `--llm "<desc>"` does
the same with an LLM-generated agent; only `--project` bootstraps a workspace.
So the most common first commands leave you with a bare agent and **no
`project.yaml`**.

A live session exposed the consequence end-to-end:
```
$ mdk init sitebot2 "answer questions about https://www.movate.com"   # bare agent, no project
$ cd sitebot2
$ mdk run sitebot '{"question":"…"}'
✗ load failed: agent path is not a directory: /Users/…/sitebot2/sitebot   # resolved as a PATH, not a name
$ mdk validate
✗ not inside a movate project (no project.yaml / policy.yaml / movate.yaml up the tree)
```
`run`/`validate` assume either a **project context** or an **explicit agent
path** (`load_agent` → `_resolve_project_root` expects `<project>/agents/<name>/`,
and raises `agent path is not a directory` for a bare name). The bare-agent
scaffold satisfies neither, so the user is stranded.

Two adjacent observations from the same session shaped D3/D5:
- **Editor launch already exists — but only for project mode.** `_init_project`
  auto-launches `code <project_root>` (`init.py:813`, TTY-gated, `--no-open`
  opt-out, falls back to macOS `open`). The `--llm`/agent paths never call it.
- **A day-stale install caused phantom "bugs."** The operator's `mdk` was
  `2026.5.26.7` while the repo was `2026.5.27.x`; it emitted `schema/*.json` and
  no RAG/crawl/verify simply because all of that merged *after* their install —
  costing real debugging time. A freshness check would have caught it instantly.

## Decisions in detail

### D1 — `mdk init` yields/extends a project (context-aware)
The default and `--llm`/`-t` paths route through the **project** scaffold:
new project outside one, add `agents/<name>/` inside one (detect via
`_resolve_project_root` upward search). The agent (template or LLM-generated, in
the canonical #127 layout) lands at `agents/<name>/`; the project gets
`project.yaml` + `AGENTS.md` + snapshot. `--bare` preserves the old
single-dir output. This is a **CLI behavior change** (CLAUDE.md compat rule 5):
shipped with a CHANGELOG entry; the context-awareness means it does the
intuitive thing, and `--bare` is the documented escape hatch. (`mdk add` is
unchanged — it already targets `agents/` inside a project.)

### D2 — name-based agent resolution in `run` / `validate` / `dev`
These accept an agent **name** that resolves to `agents/<name>/` within the
discovered project, in addition to an explicit path (unchanged). The bare-name
failure (`agent path is not a directory`) becomes a helpful message: e.g.
*"no agent 'sitebot' here — did you mean `mdk run sitebot` from the project root,
or `mdk run .` for the agent in this folder?"*. A single resolver
(name → path, with project discovery) backs all three commands. A **standalone
agent dir is first-class** (not a degraded mode): pointed at (or sitting in) a dir
with an `agent.yaml`, `mdk run .` / `validate .` / `dev .` work and no longer
demand a `project.yaml` marker — so the `--bare` / embedded-in-a-repo case is
fully supported, while a *project* remains the default for the multi-agent +
shared-skills/contexts + copilot/`AGENTS.md` story.

### D3 — one shared editor launcher
Extract the project-mode launch (`init.py:806–859`) into
`_launch_editor(path, *, open_editor)` and call it from project mode, the
`--llm`/`-t` init paths, **and** `mdk dev` (which has its own `_open_in_editor`,
`dev_cmd.py:717`) so there's one implementation. Identical gating: `code` →
fallback `open`, **TTY-only** (never CI/`--mock`), `--no-open` opt-out,
best-effort (never fails the command).

### D4 — next-steps print the exact runnable command
The success panel renders the precise command for what was created — e.g.
`cd <project> && mdk run <name> '{…}'` (or `mdk run .` for `--bare`) — built from
the actual on-disk result, so copy-paste works the first time.

### D5 — `mdk doctor` staleness check
`mdk doctor` compares the installed `mdk` version against the source of truth
(when run from / alongside an editable repo checkout, the repo's version; else a
"last-updated N days ago" surfacing) and warns when behind, with the reinstall
command. Cheap, and it converts the silent day-stale-install class of "bug" into
an obvious, self-fixing prompt.

### D6 — the `--llm` scaffold model is layered-configurable
The LLM that *powers* `--llm` (distinct from the generated agent's *runtime*
model — `_pick_target_model` keeps them separate; today `_DEFAULT_LLM_MODEL =
openai/gpt-4o-mini-2024-07-18` + a `--llm-model` flag) is resolved by precedence,
mirroring how mdk resolves credentials/config (ADR 022):
1. `--llm-model <model>` (per-invocation) — *exists*.
2. `MDK_LLM_MODEL` env var.
3. project `project.yaml: scaffold.model:`.
4. user `~/.movate/config.yaml: scaffold.model:` (set via `mdk config set scaffold.model …`).
5. built-in **key-matched** default (OpenAI key → gpt-4o-mini; Anthropic key →
   claude-haiku) — *exists* (#108).
Any LiteLLM-supported provider/model already works through the provider seam
(openai / anthropic / azure / gemini / local) — this only adds a **persistent
default** so users needn't repeat the flag. Keys for the chosen provider are
already handled by BYOK (ADR 018) + credential autoload, so there's no new auth
surface. The same `scaffold.model` precedence can later cover the generated
agent's default runtime model if desired (out of scope here).

## Consequences

**Positive**
- The first command leaves a **runnable, navigable project**: `mdk run <name>` /
  `validate` / `dev` work immediately, the editor opens, and `AGENTS.md` +
  the copilot (ADR 025) are right there.
- Eliminates the exact dead-ends the live session hit (bare agent + path-only
  resolution + no editor).
- Mostly reuses shipped pieces — the project scaffolder, `load_agent`, the
  project-mode launcher, `mdk doctor` — so the new surface is small.

**Negative / risks**
- A behavior change to `mdk init`'s default output (bare agent → project).
  Mitigated by context-awareness + the `--bare` escape hatch + CHANGELOG; `mdk
  add` (the in-project path) is untouched.
- `init.py` is touched by an in-flight PR (#479, `AGENTS.md` scaffold) — the impl
  must land **after** that to avoid churn (see Scope).
- Name↔path ambiguity in resolution (a dir literally named like an agent).
  Resolve deterministically: an existing **path** wins; else try **name** in the
  project; else the friendly error. Covered by tests.

**Test matrix (impl must cover):** `mdk init <name> "<desc>"` outside a project →
project + `agents/<name>/` + `project.yaml` + `AGENTS.md` + snapshot, and
`mdk run <name>`/`validate` work from the project root; inside a project → adds
`agents/<name>/`, no nested project; `--bare` → legacy single-dir output;
name resolution (name, path, ambiguous, not-found→friendly error) across
run/validate/dev; editor launch fires on TTY + is skipped under `--no-open`/no-TTY
/`--mock`; doctor flags a deliberately-stale version.

## Alternatives considered
- **(a) Keep the bare-agent default; just make `run`/`validate` work on a bare
  dir (`mdk run .`) + better hints.** *Rejected as the primary fix:* it doesn't
  match the "I made a project" expectation, leaves no `project.yaml` for the
  copilot/AGENTS.md/snapshot story, and `mdk run .` is unintuitive. (The `.`
  path + friendly hint is still added via D2 as a fallback, and `--bare` keeps
  this mode available.)
- **(b) A separate `mdk new <name>` for "project with agent" and leave `init`
  alone.** *Rejected:* adds a verb to learn and splits the front door; `init` is
  already the front door — make it do the right thing.
- **(c) Per-flag (`--project` vs `--agent`) with no smart default.** *Rejected:*
  the context-aware default (in-project → add; outside → new project) is more
  intuitive and is what users mean; flags remain for the explicit cases
  (`--bare`).

## Scope / rollout
One PR (the pieces interlock: init contract + name resolution + shared launcher +
next-steps + doctor). **Sequenced after the in-flight `init.py` PR (#479) merges**
to avoid conflict. D2's resolver + D3's launcher are small shared helpers;
D1 reuses the project scaffolder; D5 extends `mdk doctor`. Pairs naturally with
the ADR-025 copilot (a project is where the copilot operates).
