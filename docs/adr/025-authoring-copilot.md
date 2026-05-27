# ADR 025 — The mdk authoring copilot: an action-catalog spine for LLM-driven agent evolution

**Status:** Accepted
**Date:** 2026-05-27 (proposed); 2026-05-27 (approved, status flipped to Accepted)
**Deciders:** Engineering + Deva (Movate)
**Context window:** v1.x authoring loop — let users (and the coding agents they
already use) *evolve* an agent after `init` in natural language — add/refine
contexts, instructions, KB, skills, evals — through one safe, validated,
reversible path, instead of hand-editing YAML and re-learning ~50 verbs.
**Related / constrained by:** ADR 002 (skills + contexts are the building
blocks), ADR 016 (harvest → continuous-eval → canary; the "improve my agent"
autopilot plugs in here), ADR 021 (content-addressed versioning + redeploy
propagation = reversibility substrate), ADR 023 (auto-RAG), ADR 024 (per-step
observability = how the copilot *sees* run quality), the **canonical agent
layout (#127)** (a predictable tree is what makes safe machine editing
possible), `--llm` scaffolding (F2/F3/F5–F8), and the friendly validation errors
(#119, the copilot's error sensor). **Supersedes / absorbs:** F9 (#118,
"iterative refinement loop") — generalized here.

## Decision

Introduce a small, typed **Authoring Action Catalog** — the set of safe
operations that can mutate an agent/project (add-context, edit-instructions,
ingest-KB, add-skill, set-model, add-eval-case, …) — and a uniform
**plan → preview → apply → verify** spine around it. Then drive that **one**
catalog from **three** surfaces:

1. **(S1) Conversational `mdk dev`** — a native, provider-pluggable LLM planner
   maps natural language to catalog actions inside the existing resident loop.
2. **(S2) `AGENTS.md` in every scaffold** — teaches *external* coding agents
   (Claude Code, Cursor, …) the catalog-as-CLI-commands + the canonical tree +
   the verify loop, so the agent the user already has becomes mdk-fluent.
3. **(S3) An mdk authoring MCP server** — exposes the catalog as MCP tools
   (`plan_*` / `apply_*` / `validate` / `run`) for structured IDE/agent use.

The LLM is **not** hardcoded into the CLI: catalog actions are LLM-agnostic,
each is **validated and reversible**, and the planner is a swappable
`BaseLLMProvider`. The copilot never gets raw filesystem or shell access — it
can only compose **typed catalog actions**, every one of which goes *through*
`mdk validate`, the registry, and content-addressed versioning. This is the
control-plane authoring tool; it never lives in the runtime.

## Context

Today the building blocks already exist as discrete commands — `contexts create
--agent` (auto-attaches), `kb ingest` (file/URL/crawl), `skills scaffold`,
`validate`, `run --mock`, `eval` — and `mdk dev` already orchestrates them in a
resident **menu-driven** loop (scaffold → edit → hot-reload test → eval →
deploy; actions `r/i/c/e/g/d/k/x/o/q`). `--llm` generates the *initial* agent.
What's missing is the layer that turns *"add a returns-policy context,"*
*"make the tone more formal,"* *"ingest our docs site,"* *"add a calculator
skill"* into the right command(s) **with a feedback loop and an undo** — and a
way for the coding agents users already drive (this very project has been built
by Claude Code calling mdk) to do the same without re-deriving the workflow each
session.

Two forces make *now* the right time, and shape the design:

- **The canonical layout (#127) just made the tree predictable.** A machine
  editor can only be safe if "where does a context live / what does agent.yaml
  look like / where are the schemas" has one answer. It now does.
- **mdk already has the guardrails an LLM editor needs:** `validate` (+ friendly
  errors, #119) is a structural sensor, `run --mock` is a zero-cost smoke,
  `eval` is a quality signal, `snapshot` + content-addressed versioning
  (ADR 021) make every change reversible, `policy` can constrain actions, and
  `audit` (item 35) can log them. The copilot is mostly *orchestration over
  these*, not new engine work — the same principle that made `mdk dev` thin.

The risk we are explicitly designing against: an LLM that silently rewrites
`agent.yaml`, breaks the schema, spends money crawling, or can't be undone.

## Decisions in detail

### D1 — The Authoring Action Catalog (the core seam)
A registry of typed authoring operations. Each `AuthoringAction` declares:
`name`, an LLM-facing `description`, an input schema, `side_effects`
(`filesystem` | `network` | `cost`), `reversible: bool`, and two methods —
`plan(project, args) -> ActionPlan` (a dry-run diff + cost/side-effect estimate,
**no writes**) and `apply(project, args) -> ActionResult` (executes via the
**existing** primitives). Initial catalog (all reuse shipped functions):
add/edit/remove **context** (`attach_context_to_agent`), edit **instructions**
(`prompt.md`), set/swap **model** + add fallback, **ingest KB**
(file/URL/crawl), add **skill** (built-in or `skills scaffold`), set
**retrieval** (`auto_into`, ADR 023), add/edit **eval case**, describe/rename
agent, add an **agent** to the project (`add`/`init`), compose a **workflow**
(`compose`). New capabilities extend the catalog — never a new hardcoded LLM
prompt. This is the single source of truth all three surfaces share (the lesson
this codebase keeps relearning: one shared path, no drift).

### D2 — plan → preview → apply (the safety spine)
Every action is two-phase. The copilot first **plans** (renders a unified diff /
summary + a cost + side-effect estimate), shows it, and **applies only on
confirmation**. Confirmation policy by class: **additive + reversible + free**
(add context, add eval case) may auto-apply in an opt-in "fast mode";
**cost-incurring or networked** (KB crawl, model swap) and **destructive**
(remove/replace) **always** require an explicit yes — mirroring mdk's existing
explicit-permission boundaries. The LLM proposes; a human or a policy gates.

### D3 — verify-and-self-correct (robustness)
After `apply`, the spine auto-runs **`validate` → `run --mock` → (optional)
`eval`** and surfaces results inline (using ADR 024's per-step tree for
"what did this run do"). On a `validate` failure the planner **re-plans** up to
N attempts using the friendly error (#119) as the signal; if it still can't
produce a valid tree, it **reverts** (D4) and reports. This closes the
"did I break it?" loop that pure LLM editing lacks.

### D4 — checkpoints + `undo` (reversibility)
Each `apply` first takes a **checkpoint** (reuse `mdk snapshot` + the
project-state snapshot that `init --project` already creates). `mdk dev`/the
copilot gains `undo` (revert the last action) and `history` (the action log).
Content-addressed versioning (ADR 021) means a reverted edit is a clean no-op.
Nothing the copilot does is one-way.

### D5 — three surfaces, one catalog (no fragmentation)
- **S1 — conversational `mdk dev`:** add a `chat`/`copilot` action to the
  existing `_actions_menu`; NL intent → planner → catalog action(s) → D2/D3/D4.
  Batteries-included; works with no external tool. **This is F9, generalized.**
- **S2 — `AGENTS.md` scaffolded into every project** (the emerging cross-agent
  convention): documents the catalog **as CLI commands**, the canonical layout
  (#127), and the verify loop, so any coding agent drives mdk via its own
  intelligence. *Cheapest, highest leverage — ship first.* A repo-level
  `AGENTS.md` also helps contributors + sessions like the one that built this.
- **S3 — `mdk mcp serve` authoring server:** exposes the catalog as MCP tools
  with the plan→apply safety built in. (mdk already has the MCP *skill backend*,
  `core/skill_backend/mcp.py`; this is the inverse — mdk exposing *its* ops.)

### D6 — provider-pluggable, self-documenting, grounded planner
The planner is a `BaseLLMProvider` (swappable; `--mock` planner returns scripted
actions so the whole copilot is testable + CI-runnable with no keys). Its system
prompt is **generated from the catalog** (actions describe themselves → no
hand-maintained tool list) plus the **current project state** (the tree,
`agent.yaml`, existing contexts/skills) so it is grounded in *this* project, with
a cacheable prefix (per #109). Ambiguous requests trigger **one structured
clarifying question** (a `needs_clarification` outcome) rather than a silent
guess.

### D7 — creative capabilities (built on existing seams, opt-in)
- **"Improve my agent" autopilot:** run `eval`, read the failures + ADR 024
  per-step costs, and propose targeted edits (instruction tweaks, a missing
  context, added eval cases) — directly wiring ADR 016's harvest→eval loop into
  authoring.
- **Grounding-gap detection:** if an agent is RAG-shaped but its KB is empty
  (exactly what the live smoke surfaced), proactively offer to ingest (F7/F8).
- **Project-level scaffolding:** from a high-level description, lay out a whole
  tree — multiple agents + shared contexts/skills + a workflow — via `add` /
  `compose`.
- **Cost & side-effect budgeting:** actions' declared `cost`/`side_effects` feed
  an estimate the user (or `policy`) caps ("this crawl fetches ~25 pages + embeds,
  ~$0.05 — proceed?").
- **Audit + replay:** every applied action is logged (reuse item 35 audit
  telemetry) → a reviewable changelog of what the LLM did, and a replayable
  transcript.

### D8 — policy gates + hard boundaries (what this rules OUT)
- The copilot composes **only** catalog actions — **no raw filesystem writes, no
  shell, no arbitrary code execution.** Every mutation flows through
  `validate` + the registry + versioning.
- It **never** auto-deploys, touches credentials, or runs `az` without explicit
  confirmation (the existing explicit-permission rules hold unchanged).
- Project **`policy`** can restrict the catalog per project (e.g. forbid model
  swaps or network crawls, require confirmation for class X).
- It is **not** a replacement for `--llm` (initial scaffold) — `--llm` *creates*,
  the copilot *evolves*; they compose.
- **cli ⊥ runtime** preserved: this is a control-plane authoring tool; nothing
  here ships in the runtime/execution plane.

## Consequences

**Positive**
- One natural-language path to evolve an agent — contexts, instructions, KB,
  skills, evals — with diff-preview, a verify loop, and undo.
- Meets users where they are: the coding agent they already run (Claude Code,
  Cursor) becomes mdk-fluent via `AGENTS.md`, *and* a native copilot exists for
  those without one — over the **same** catalog, so behavior can't diverge.
- Mostly orchestration over shipped primitives + guardrails (validate / mock /
  eval / snapshot / policy / audit) — small new engine surface, big UX leap.
- Safe by construction: typed reversible actions, plan-before-apply, validate
  self-correct, policy gates — an LLM editor that can't silently break or
  un-undo the project.

**Negative / risks**
- A genuinely new product surface (catalog + planner + 3 consumers) — scoped
  into independently-shippable PRs (below) to bound blast radius.
- Planner quality: a wrong action mapping. Mitigated by plan-preview + confirm +
  validate/mock verify + undo (a bad plan is caught before or reverted after).
- LLM cost of the planner itself: bounded by `--mock` for tests, a cacheable
  prompt prefix, and per-session budget (reuse budget/policy).
- Another agent.yaml-touching writer. Mitigated: it reuses the **canonical
  writer** from #127 (one writer, already the standard) — it does not invent a
  parallel one.

**Test strategy (impl must cover):** each catalog action's `plan` produces a
correct diff with no writes and `apply` mutates via the primitive + passes
validate; the verify loop reverts on an injected validate failure; `undo`
restores the prior checkpoint exactly; the `--mock` planner makes the whole
copilot run hermetically (no keys); a destructive/networked action refuses to
auto-apply without confirmation; policy can forbid an action class; `AGENTS.md`
is scaffolded and its documented commands actually exist.

## Scope / rollout (this ADR decides; impl is sequenced PRs)
- **PR1 — the catalog + spine.** `AuthoringAction` protocol + initial catalog +
  plan/apply/verify + checkpoint/undo, usable programmatically and from the CLI.
  No LLM yet. (Foundation everything else builds on.)
- **PR2 — `AGENTS.md` scaffold.** Cheapest user value; makes external coding
  agents mdk-fluent immediately. + a repo-level `AGENTS.md`.
- **PR3 — conversational `mdk dev` (S1).** The native planner over the catalog
  (this is F9, #118).
- **PR4 — `mdk mcp serve` authoring server (S3).**
PR1 unblocks PR2–PR4; each is independently shippable and testable.

## Alternatives considered
- **(a) Hardcode an LLM editing loop directly in `mdk dev`** (no catalog seam).
  *Rejected:* couples the LLM to the CLI, can't be reused by the MCP server or
  tested without a model, and tends toward ad-hoc raw edits. The catalog seam is
  the difference between "a chat box that edits YAML" and a safe, reusable,
  testable authoring layer.
- **(b) `AGENTS.md` only — rely entirely on external agents.** *Rejected as the
  end-state* (kept as PR2, the first slice): leaves users without Claude
  Code/Cursor unserved and gives the external agent no *typed, reversible* safety
  rails — it would edit files raw. The catalog + MCP (S3) give external agents
  the same plan→apply guarantees.
- **(c) Give the copilot raw filesystem/shell access and let it "just edit."**
  *Rejected:* no validation gate, no reversibility, no policy, no audit —
  precisely the footguns D2/D4/D8 exist to prevent. The whole value is the
  typed, reversible, validated catalog.
- **(d) A runtime-hosted authoring agent (as a `kind: Agent`).** *Deferred:*
  conceptually elegant (mdk building mdk projects) but it would pull authoring
  into the execution plane and need filesystem/registry write access from the
  runtime — a boundary violation. Authoring stays control-plane; revisit only if
  hosted/multi-tenant authoring becomes a product need.
