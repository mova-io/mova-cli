"""``movate init`` — the front door: always leave a runnable project.

``mdk init`` is **context-aware** (ADR 026). What it produces depends on
where you run it and whether you describe an agent to scaffold:

* **Outside a project** → bootstrap a runnable PROJECT: ``project.yaml`` +
  ``AGENTS.md`` + ``.env.example`` + ``.gitignore`` + ``agents/`` + an
  initial ``.mdk/snapshots/`` baseline. With a template (``-t``), an
  ``--llm "<description>"``, or a positional description, the agent is
  scaffolded under ``agents/<name>/`` so ``mdk run <name>`` works from the
  project root immediately. Bare ``mdk init <name>`` (no template / no
  description) leaves the project with an empty ``agents/`` ready for
  ``mdk add``. ``AGENTS.md`` is the cross-agent onboarding file (ADR 025) —
  it teaches a coding agent (Claude Code, Cursor, …) how to evolve THIS
  project. The snapshot is the baseline for ``mdk diff`` / ``mdk rollback``.

* **Inside a project** → ADD the agent under ``agents/<name>/`` of the
  current project (the same place ``mdk add`` targets). No nested project.

* ``--bare`` → a STANDALONE single-dir agent at ``<target>/<name>/`` (no
  ``project.yaml`` / ``agents/`` wrapper) — the escape hatch for dropping an
  agent into a non-mdk repo or a quick throwaway. First-class, not a
  degraded mode: ``mdk run .`` / ``validate .`` / ``dev .`` all work on it.

* ``--project`` → explicitly bootstrap just the project workspace (back-compat
  flag; the same workspace the outside-a-project default produces).

**``--llm "<description>"``** generates the agent from a natural-language
description. The generator (in :mod:`movate.scaffold`) calls the configured
provider, parses the response into a :class:`GeneratedAgent`, writes it to a
tempdir, and validates by loading it back through :func:`load_agent`; on
validation failure the error is fed back to the LLM for one retry, a second
failure stashes the raw payload at ``.mdk/llm-init-failed-<name>.json`` and
exits 1. It is **shape-aware** (Q&A / classifier / extractor / RAG / tool-use)
and emits ``agent.yaml`` + ``prompt.md`` + schemas + seed eval cases. When the
description contains a URL it auto-crawls + ingests that source into the new
agent's KB then runs a grounded verify; a tool-use intent scaffolds a runnable
skill STUB; and under ``--mock`` it runs a post-scaffold eval baseline. Pair
with ``--mock`` for hermetic CI (no API keys); ``--dry-run`` previews without
writing; the scaffolder model is layered-configurable (``--llm-model`` >
``MDK_LLM_MODEL`` > project ``scaffold.model`` > ``mdk config set
scaffold.model`` > a key-matched default). Successful scaffolds emit a Rich
Panel + a greppable ``mdk_init_summary:`` line for CI parity.

``mdk dev <name>`` is the guided edit→test→deploy loop that pairs with this;
``mdk demo`` is a fully populated reference project (project + agent + dataset).
"""

from __future__ import annotations

import contextlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from movate.core.agent_schema_utils import check_adr023_retrieval as _check_adr023_retrieval
from movate.core.config import PROJECT_MARKER_FILES as _PROJECT_MARKERS
from movate.core.paths import project_state_dir
from movate.templates import get_template_path, list_templates

console = Console()
err_console = Console(stderr=True)


# Project-mode files. Kept inline (not separate templates) for the same
# reason `mdk demo` does — they're tiny and inlining keeps the recipe
# legible in one read. If they grow, lift to src/movate/templates/.
#
# Body MUST validate as :class:`movate.core.config.ProjectConfig`
# (``extra="forbid"``) so a freshly-bootstrapped project's first
# ``mdk validate`` call doesn't trip on schema noise. The project
# metadata (name / description) lives in the file comment header
# rather than in the YAML body — docs/runbook reads ``root.name`` as
# the fallback when these aren't set, so we preserve the readable
# project identity without breaking strict validation.
_PROJECT_MOVATE_YAML = """\
# =============================================================================
# {name} — movate project config
# =============================================================================
#
# Read this file top to bottom — it's the canonical reference for what
# you can configure at the project level. Every block below is
# documented in-place. Active blocks are uncommented; the rest ship
# commented-out so you can enable a feature by deleting `#` rather
# than copy-pasting from external docs.
#
# Filename history (all three still load — picked in this order):
#   1. `project.yaml` — canonical (May 2026+)
#   2. `policy.yaml`  — legacy v1.x; loads with a deprecation warning
#   3. `movate.yaml`  — original v0.x; loads with a deprecation warning
#
# Layering: per-agent `agent.yaml` ALWAYS wins per-key; entries here
# only fill the gaps an agent didn't specify. Same for contexts: an
# `agents/<name>/contexts/<file>.md` overrides
# `contexts/<file>.md` when names collide.
#
# Run `mdk doctor` to see the merged config any specific agent
# resolves to. Run `mdk validate` to gate-check this file + every
# agent against the active policy.
#
# =============================================================================


# -----------------------------------------------------------------------------
# Project layout — where mdk looks for things
# -----------------------------------------------------------------------------
# Relative paths resolved from this file's location. Change these if
# you want a non-default folder name (rare).

agents_dir: ./agents
workflows_dir: ./workflows
skills_dir: ./skills
contexts_dir: ./contexts
# kb/ has no project-config field — it's resolved by convention via
# `movate.core.kb_loader.resolve_kb_file(name)`. Drop data at
# `./kb/<filename>` and skills like `kb-lookup` find it automatically.


# -----------------------------------------------------------------------------
# Defaults applied to every agent
# -----------------------------------------------------------------------------
# Three layered groups:
#   * model.params — LiteLLM-style per-call params (temperature, etc.)
#   * timeouts     — per-call + total-run caps
#   * budget       — soft cost cap per run (hard cap lives in `policy:`)
#
# Per-agent `agent.yaml` always wins per-field. Headline use: pin
# temperature once here so every agent runs deterministically without
# repeating the same line in each agent.yaml.

defaults:
  model:
    params:
      temperature: 0.0
      max_tokens: 512
  # Uncomment to set workspace-wide timeouts (agent.yaml fields win
  # per-field; comment out + omit to use the executor's defaults):
  # timeouts:
  #   call_ms: 15000      # per-LLM-call cap
  #   total_ms: 60000     # whole-agent-run cap
  #
  # Uncomment to set workspace-wide budget defaults:
  # budget:
  #   max_cost_usd_per_run: 0.05   # SOFT default; policy.max_cost is HARD


# -----------------------------------------------------------------------------
# Policy gates — uncomment any block to enforce workspace-wide
# -----------------------------------------------------------------------------
# Hard gates checked by `mdk validate` BEFORE any LLM call. A policy
# violation exits 2 — operators can't accidentally ship an agent that
# breaks the org's rules. Empty / commented = permissive.

# policy:
#   # Whitelist of provider prefixes (before the `/` in a LiteLLM model
#   # string). Agents using anything else fail validate.
#   allowed_providers:
#     - openai
#     - anthropic
#     - azure
#
#   # Blacklist — overrides allowed_providers. Use for specific
#   # models you've banned (e.g. an old model with a known bug).
#   denied_providers:
#     - cohere
#
#   # HARD ceiling on per-run cost. Agents can't override above this.
#   max_cost_usd_per_run: 0.50
#
#   # Default fallback when an agent's primary model fails. Each
#   # entry is a LiteLLM-style provider string. Agents can declare
#   # their own `model.fallback` to override per-agent.
#   fallback_chain:
#     - openai/gpt-4o-mini-2024-07-18
#     - anthropic/claude-haiku-4-5-20251001


# -----------------------------------------------------------------------------
# Runtime gate — which AgentRuntime values are allowed
# -----------------------------------------------------------------------------
# Default permissive (any installed runtime). Pin to a subset to
# enforce architectural direction. Uncomment to enable:

# runtime:
#   allowed:
#     - litellm
#     # - native_anthropic
#     # - native_openai
#     # - langchain
#     # - lyzr


# -----------------------------------------------------------------------------
# Skill side-effect gate — which categories of skills are allowed
# -----------------------------------------------------------------------------
# Restricts agents to skills whose `side_effects:` field is in the
# allowed list. The four categories:
#   * read-only       — opens files / reads remote APIs, no writes
#   * network         — outbound HTTP requests
#   * filesystem      — writes to the local disk
#   * mutates-state   — kills processes, deletes data, etc.

# skills:
#   allowed_side_effects:
#     - read-only
#     # - network
#     # - filesystem
#     # - mutates-state


# -----------------------------------------------------------------------------
# Eval defaults — used by `mdk eval` + `mdk ci eval`
# -----------------------------------------------------------------------------
# Pin the gate threshold + runs-per-case + judge model once here so
# CI uses the same values every team member uses locally. Per-call
# CLI flags override.

# eval:
#   gate: 0.7                                  # `mdk eval --gate <N>` default
#   runs: 3                                    # # of runs per case for stability
#   judge: openai/gpt-4o-mini-2024-07-18       # cross-family preferred


# -----------------------------------------------------------------------------
# Bench defaults — used by `mdk bench`
# -----------------------------------------------------------------------------
# Default provider matrix for multi-model comparison runs. Agents
# can override per-call.

# bench:
#   providers:
#     - openai/gpt-4o-mini-2024-07-18
#     - anthropic/claude-haiku-4-5-20251001
#     - azure/gpt-4.1


# =============================================================================
# About .mdk/ — runtime state directory
# =============================================================================
#
# Created in the project root when you run any `mdk` command that
# needs persistent state. Layout:
#
#   .mdk/
#   ├── local.db           — SQLite for runs + failures (gitignored)
#   ├── snapshots/         — content-addressed snapshots of project state
#   │   └── <hash>/        — immutable: agent.yaml + prompt.md + schemas
#   │       ├── manifest.json
#   │       └── <files>
#   └── baselines/         — `mdk eval --baseline` stored eval scores
#
# Snapshots are the central operational primitive:
#
#   * `mdk snapshot create`     — capture current state
#   * `mdk diff <a> <b>`        — what changed between two snapshots?
#   * `mdk rollback <hash>`     — restore project state to a prior snapshot
#   * `mdk audit`               — scan snapshots for drift / dangling refs
#   * `mdk promote --from <h>`  — copy a tested snapshot dev → staging
#
# Snapshots are content-addressed (the directory name IS the SHA-256
# of the manifest), so re-snapshotting identical state produces the
# same hash. They're small + git-friendly by default; `.gitignore`
# tracks them so your repo carries a verifiable history of "what
# shipped when". Drop `.mdk/snapshots/` from `.gitignore` if you'd
# rather treat them as machine-local.
"""

_PROJECT_ENV_EXAMPLE = """\
# Provider keys. Set at least one of:

OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
# AZURE_API_KEY=

# Optional — enables Langfuse tracing if set:
# LANGFUSE_PUBLIC_KEY=
# LANGFUSE_SECRET_KEY=
"""

_CONTEXTS_README = """\
# `contexts/` — Reusable Prompt Contexts

Markdown files in this directory get **prepended to agent prompts**
at runtime. The pattern lets you DRY up shared instructions across
multiple agents — tone guides, output rubrics, persona definitions
— without copy-pasting into every `prompt.md`.

## What goes here

| Path | Purpose |
|---|---|
| `contexts/<name>.md` | Project-level shared context |
| `agents/<agent>/contexts/<name>.md` | Per-agent override (wins on collision) |

The base name (no extension) is the context's **id**. An agent
declaring `contexts: [support-tone]` resolves to
`contexts/support-tone.md` (project-level) — or to
`agents/<that-agent>/contexts/support-tone.md` when present, which
wins per-agent.

## Conventions

- **Keep them short and focused.** A context is a *fragment*, not a
  full prompt. One rubric, one tone guide, one persona — combined
  with the agent's own `prompt.md` at runtime.
- **No frontmatter required.** Just plain Markdown. The loader
  reads the file verbatim.
- **Naming is hyphen-cased.** `support-tone.md`, `triage-rubric.md` —
  matches the rest of `mdk`'s `kebab-case` identifiers.
- **Per-agent overrides win on collision.** If both
  `contexts/triage-rubric.md` (project) and
  `agents/ticket-triager/contexts/triage-rubric.md` (per-agent) exist,
  the per-agent one is used for `ticket-triager` only. Run
  `mdk doctor agent <name>` to see which tier each context resolved to.

## Examples that ship with templates

- `support-tone.md` — auto-scaffolded by `mdk add ticket-triager`,
  defines the customer-facing tone for support responses.
- `triage-rubric.md` — auto-scaffolded by the same template,
  defines priority + category criteria.
- `grounded-qa-rubric.md` — auto-scaffolded by `mdk add rag-qa`,
  defines citation + grounding requirements.

## See also

- `mdk doctor agent <name>` — shows resolved context paths per agent.
- `agents/<name>/agent.yaml` — declare which contexts the agent uses.
"""


_SKILLS_README = """\
# `skills/` — Reusable Skill Definitions

Skills are **callable tools** an agent can invoke at inference time:
Python functions, HTTP endpoints, or MCP tools. The pattern lets
multiple agents share the same tool registry instead of redefining
it per agent.

## What goes here

Each skill is a directory:

```
skills/<skill-name>/
├── skill.yaml      # contract: name, backend, side_effects, schemas
├── impl.py         # Python backend (one of three options)
├── README.md       # optional — explains the skill's purpose
└── corpus.json     # optional — data the skill reads at runtime
```

`skill.yaml` declares the **backend** (`python` | `http` | `mcp`),
the **side-effects category** (`pure` | `network_read` | `network_write` |
`filesystem` | `shell`), and the **input/output schemas**.

| Backend | When to use |
|---|---|
| `python` | Local logic, fast iteration, no network. `impl.py` defines `def run(input) -> output`.|
| `http` | Wraps an existing REST API. Set `endpoint:` in `skill.yaml`. |
| `mcp` | Plugs into a Model Context Protocol server. Set `mcp_server:` in `skill.yaml`. |

## Conventions

- **Skill names are hyphen-cased.** `web-search`, `kb-lookup`,
  `lint-runner` — agents reference them by name in `agent.yaml`'s
  `skills: [<name>]` list.
- **One skill per directory.** Don't bundle multiple skills in one
  folder; the loader scans `skills/*/skill.yaml` and treats each
  dir as one skill.
- **Schemas matter.** `skill.yaml` declares input + output JSON Schema.
  The runtime validates calls before invoking `impl.py`, so a
  malformed agent request fails fast instead of mid-skill.
- **Side-effects gate is enforced.** `SkillPolicy` in `project.yaml`
  can deny entire categories (e.g. block all `network_write` skills
  for compliance). Skills MUST declare their category honestly.

## Examples that ship with templates

- `web-search` — auto-scaffolded with `mdk add rag-qa`; wraps
  DuckDuckGo HTML scrape (network_read).
- `kb-lookup` — auto-scaffolded with `mdk add ticket-triager`; reads
  from `kb/*.json` corpora (filesystem). See the `kb/README.md`
  for the corpus shape.
- `lint-runner` — auto-scaffolded with `mdk add code-reviewer`;
  shells out to `ruff check` (shell category).

## Per-agent override pattern

A project-level skill can be overridden per-agent the same way
contexts can:

```
skills/web-search/                       # project-level (default)
agents/rag-qa/skills/web-search/         # per-agent override
```

The per-agent version wins for that one agent. `mdk doctor agent
<name>` shows which tier each skill resolved to.

## See also

- `mdk skills list` — every skill discovered in the project.
- `mdk skills run <name> '<input-json>'` — invoke a skill directly,
  no agent wrapper, for debugging.
- `agents/<name>/agent.yaml` — declare which skills the agent uses.
"""


_MOVATE_STATE_README = """\
# `.mdk/` — Runtime State Directory

This directory is created and managed by `mdk`. It holds runtime state
that is intentionally separate from your source-controlled agent
definitions.

## What lives here

```
.mdk/
├── local.db           — SQLite: run history, eval results, failures
├── snapshots/         — content-addressed snapshots of project state
│   └── <sha256>/      — immutable: agent.yaml + prompt.md + schemas
│       ├── manifest.json
│       └── <files...>
└── baselines/         — eval baselines stored by `mdk eval --baseline`
```

## Snapshots — the central operational primitive

Snapshots are **content-addressed** and **immutable**: the directory
name IS the SHA-256 of the manifest, so re-snapshotting identical
state produces the same hash. They record exactly what shipped — agent
definitions, prompts, and schemas — at the moment you captured them.

Key commands:

| Command | What it does |
|---|---|
| `mdk snapshot create` | Capture current project state |
| `mdk diff <a> <b>` | What changed between two snapshots? |
| `mdk rollback <hash>` | Restore project state to a prior snapshot |
| `mdk audit` | Scan snapshots for drift or dangling refs |
| `mdk promote --from <hash>` | Copy a tested snapshot dev → staging |

## `.gitignore` policy

The project `.gitignore` ships with these entries:

```
.mdk/local.db          # machine-local run history
.mdk/baselines/        # machine-local eval baselines
# .mdk/snapshots/      # UNCOMMENT to treat snapshots as machine-local
```

`local.db` is always gitignored — it contains run history and
credentials that must not go into source control. Snapshots are
tracked by default so your repo carries a verifiable history of
"what shipped when". Uncomment the `snapshots/` entry if you prefer
to treat them as machine-local (e.g. you use a separate artifact
store for snapshot archival).
"""


_KB_README = """\
# `kb/` — Knowledge Assets

This directory holds reusable knowledge artifacts used by agents +
skills. Pre-populated by `mdk init --project` as a placeholder; the
conventions below are what `mdk` expects but you can layer your own.

## What goes here

| File shape | Used by |
|---|---|
| `*.json` | `kb-lookup` skill + custom Python skills (structured corpora) |
| `*.md` / `*.txt` | RAG-style skills (long-form documents) |
| `*.pdf` / `*.docx` | Future Tier 3 RAG with chunker + embedder |
| `embeddings/*.parquet` | Future vector-store skills (pre-computed embeddings) |

## Conventions

- **Filenames are stable.** Skills reference KB files by relative
  path (`kb/<filename>`); rename = breakage.
- **One source of truth per topic.** Don't fork `support-tickets.json`
  into `support-tickets-v2.json`; use git or `mdk snapshot` for
  versioning instead.
- **kb/ is committed by default** (the `.gitignore` shipped with
  the project tracks it). Uncomment the `.gitignore` entry to treat
  it as machine-local — useful when corpora contain PII you can't
  put in git.

## Built-in skill that uses kb/

`kb-lookup` (auto-scaffolded when an agent declares
`skills: [kb-lookup]`) ships with a small mock `corpus.json` for
demo purposes. To use your real KB, replace that file with your
own JSON in the same shape, or update `impl.py` to point at a
real search service.
"""


_PROJECT_GITIGNORE = """\
# movate runtime state — never commit
.mdk/local.db
.mdk/local.db-*

# Snapshots are commit-friendly by default (content-addressed,
# small) but operators can opt out of tracking them in git:
# .mdk/snapshots/

# Python
__pycache__/
*.pyc

# Editor / OS
.vscode/
.idea/
.DS_Store

# Secrets
.env
"""


# Project-root AGENTS.md — the cross-agent onboarding file (ADR 025).
#
# `AGENTS.md` is the emerging tool-agnostic convention (Claude Code,
# Cursor, etc. all read it) — like CLAUDE.md but not Claude-specific.
# This one teaches a coding agent how to evolve THIS mdk project: the
# canonical agent layout (#127), the authoring-command catalog, the
# feedback loop to run after edits, and the guardrails to respect.
#
# Every command + flag below MUST resolve in the Typer app — a guard
# test (`tests/test_init_agents_md.py`) walks the catalog and fails if
# any referenced command stops existing, so this file can't drift into
# documenting commands that don't exist. Keep it factual; do not add a
# command here without verifying it (and its flags) against `mdk`.
#
# `{name}` is the project name (folder/identity). Substituted at write
# time via ``.format(name=...)``, same as ``_PROJECT_MOVATE_YAML``.
_PROJECT_AGENTS_MD = """\
# AGENTS.md — guide for AI coding agents working in `{name}`

This is an **mdk** (`movate-cli`) project: a workspace of AI agents you
build, evaluate, and deploy with the `mdk` CLI. This file teaches a
coding agent (Claude Code, Cursor, …) how to evolve the agents in this
project safely. It is tool-agnostic — `AGENTS.md` is the cross-agent
convention; everything here applies whatever assistant is reading it.

Prefer the `mdk` commands below over hand-editing files: they keep the
canonical layout intact, auto-attach references, and validate as they
go. Reach for a raw editor only for `prompt.md` (the agent's
instructions) — that's the one file you're *meant* to write by hand.

## Canonical agent layout

Every agent `mdk` scaffolds uses ONE on-disk shape, regardless of how
it was created (`mdk init <name>`, `mdk add <role>`, or
`mdk init <name> --llm "<description>"`):

```
agents/<agent>/
  agent.yaml          # model, timeouts, budget, tags; schema via ./schema/*.yaml
  prompt.md           # the agent's instructions — edit this by hand
  evals/
    dataset.jsonl     # eval cases (one JSON object per line)
    judge.yaml.example # copy to judge.yaml to enable an LLM judge
  schema/
    input.yaml        # input contract (YAML JSON-Schema 2020-12)
    output.yaml       # output contract
```

Other things live at the **project** level, shared across agents:

| Path | What it holds |
|---|---|
| `contexts/<name>.md` | Reusable prompt fragments prepended at runtime |
| `agents/<agent>/contexts/<name>.md` | Per-agent context override (wins on collision) |
| `skills/<name>/` | Callable tools (`skill.yaml` + `impl.py`) |
| `agents/<agent>/kb/` | Per-agent knowledge-base documents for retrieval |
| `project.yaml` | Project config + defaults + policy gates |

Both schema forms load identically: the inline shorthand in
`agent.yaml` (`schema: {{ input: {{ text: string }} }}`) for tiny
2-to-3-field contracts, and the `schema/*.yaml` files above (what the
scaffolders emit). Pick whichever fits the contract.

## Authoring command catalog

Run all of these **from the project root** (the directory holding
`project.yaml`). `<agent>` is a directory name under `agents/`.

| Command | What it does |
|---|---|
| `mdk add <template> [--name <n>]` | Scaffold a new agent from a role template into `agents/`. |
| `mdk contexts create <name> --agent <agent>` | Create a context fragment + auto-attach it. |
| `mdk kb ingest <agent> <path-or-url> [--crawl]` | Ingest a file, dir, or URL into the KB. |
| `mdk skills scaffold <name>` | Scaffold a new skill under `skills/<name>/`. |
| `mdk validate <agent>` | Static-check the agent (schema, policy, references) — no LLM call. |
| `mdk run <agent> --mock '<json>'` | Zero-cost smoke run with the deterministic mock provider. |
| `mdk eval <agent> --mock` | Run the eval dataset under the mock provider — no API spend. |
| `mdk dev <agent>` | Resident edit → test loop: re-validates and re-runs as you edit. |

`mdk kb ingest`'s `--crawl` only applies to a URL source — it follows
links to ingest a small site rather than a single page.

To change an agent's behavior, **edit its `prompt.md`** — that file is
the agent's instructions and is meant to be hand-authored.

## The feedback loop (run after every edit)

After changing a `prompt.md`, `agent.yaml`, schema, or context, run
this loop to confirm the agent still loads and behaves — all of it
zero-cost (`--mock` never calls a provider):

```
mdk validate <agent>            # 1. does it still load + pass policy?
mdk run <agent> --mock '<json>' # 2. does a single mock run succeed?
mdk eval <agent> --mock         # 3. does the eval dataset still pass?
```

Or run `mdk dev <agent>` to have that loop run automatically as you
save files.

## Guardrails (read before you touch anything)

- **Never write to `~/.movate/`.** That's the global state dir
  (credentials, machine-local config). This project's runtime state
  lives in `.mdk/` inside the project, managed by `mdk` — don't
  hand-edit it either.
- **Use `--mock` for smoke tests.** It uses a deterministic offline
  provider, so `mdk run`/`mdk eval` cost nothing and need no API key.
  Drop `--mock` only when you intend to spend real tokens.
- **Run `mdk` from the project root** (where `project.yaml` is). The
  commands resolve `agents/`, `skills/`, `contexts/`, and `kb/`
  relative to it.
- **Prefer the commands above over hand-editing.** `mdk add`,
  `mdk contexts create`, `mdk skills scaffold`, and `mdk kb ingest`
  keep the canonical layout and references consistent. The one file
  you *should* edit by hand is `prompt.md`.
- **Both schema forms are valid.** Inline `schema:` in `agent.yaml`
  and `schema/*.yaml` files both load — don't "fix" one into the
  other.

## See also

- `project.yaml` — project config, defaults, and policy gates (read
  top-to-bottom; every block is documented in place).
- `mdk doctor agent <agent>` — shows the merged config + resolved
  contexts/skills an agent uses.
- `mdk add --list` — the catalog of role templates you can scaffold.
"""


# Env-var names every LiteLLM-backed provider checks for credentials.
# Kept in sync with the same list in :mod:`movate.cli.doctor` — adding a
# provider here means adding it there too.
_PROVIDER_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "LYZR_API_KEY",
)


def _has_any_provider_key() -> bool:
    """True if at least one provider API key is set in the environment.

    Used by ``--llm`` mode (without ``--mock``) to fast-fail with a
    friendly error instead of crashing deep inside the LLM call. We
    don't try to match the KEY to the chosen model — most operators
    have ONE key set, and any wrong-provider mismatch surfaces with a
    clearer error from LiteLLM downstream.
    """
    import os  # noqa: PLC0415

    return any(os.environ.get(k, "").strip() for k in _PROVIDER_KEY_ENV_VARS)


def _cd_target(project_root: Path) -> str:
    """Pick the right ``cd`` argument for the success Panel's next-steps
    block.

    Returns:

    * The project's name (e.g. ``support-bot``) when ``project_root``
      is a direct child of cwd — the common case when the operator
      omitted ``--target`` / ``--at`` and the project lands at
      ``./support-bot/``. Copy-paste-friendly without an absolute path.
    * The absolute path when ``project_root`` is outside cwd — e.g.
      when the operator passed ``--at ~/work``, the panel should say
      ``cd /Users/.../work/support-bot`` so the line works as-is no
      matter where they ran ``mdk init`` from.
    """
    try:
        rel = project_root.relative_to(Path.cwd())
    except ValueError:
        # project_root is outside cwd → absolute path is the only safe
        # copy-paste target.
        return str(project_root)
    rel_str = str(rel)
    # rel == "." would happen if the operator bootstrapped in place —
    # the cd line is nonsense there; fall back to absolute.
    return rel_str if rel_str != "." else str(project_root)


def _is_in_project() -> bool:
    """Walk up from cwd looking for ``movate.yaml`` — the same
    convention :mod:`movate.cli.add_cmd` uses. Lets ``mdk init``
    surface a context-aware hint when called outside a project.
    """
    from movate.core.config import is_project_root  # noqa: PLC0415

    current = Path.cwd().resolve()
    while True:
        if is_project_root(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _launch_editor(path: Path, *, open_editor: bool, mock: bool = False) -> bool:
    """Best-effort, TTY-gated launch of an editor on ``path`` (ADR 026 D3).

    The single editor-launch implementation shared by project mode, the
    ``--llm`` / ``-t`` init paths, and ``mdk dev`` (which previously had its
    own ``_open_in_editor``). Identical gating in one place:

    * Honors ``--no-open`` via ``open_editor=False`` (operator opt-out).
    * TTY-ONLY — never launches a GUI under CI / piped stdout / ``--mock``
      (a mock/hermetic run must stay headless).
    * Editor pick: ``$EDITOR`` (if set) → VS Code (``code``) → Cursor
      (``cursor``) → macOS Finder (``open``). The Finder fallback is the
      "reveal it" last resort; we still launch it (the operator asked) but
      report it as a reveal.
    * BEST-EFFORT: a launch failure (no editor, OSError) is swallowed —
      it NEVER fails the calling command. The caller falls back to its
      printed next-steps.

    Returns ``True`` when an editor process was actually spawned, so the
    caller can suppress a redundant "open it manually" menu pick. Returns
    ``False`` for every skip / failure path.
    """
    import os as _os  # noqa: PLC0415
    import shutil as _shutil  # noqa: PLC0415
    import subprocess as _subprocess  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415

    # Opt-out + headless gates. --mock implies a hermetic run (CI / offline),
    # so never launch even on a TTY; same for a non-tty stdout (piped / CI).
    if not open_editor or mock or not _sys.stdout.isatty():
        return False

    argv: list[str] | None = None
    label: str | None = None
    editor_env = _os.environ.get("EDITOR", "").strip()
    if editor_env:
        # An explicit $EDITOR (vim, nvim, emacs, a wrapper) wins over the
        # GUI auto-detect — the operator's chosen tool is authoritative.
        argv, label = [*editor_env.split(), str(path)], editor_env.split()[0]
    elif _shutil.which("code"):
        argv, label = ["code", str(path)], "VS Code"
    elif _shutil.which("cursor"):
        argv, label = ["cursor", str(path)], "Cursor"
    elif _shutil.which("open"):  # macOS Finder fallback
        argv, label = ["open", str(path)], "Finder"

    if argv is None:
        return False

    try:
        # Detach from the CLI's lifecycle (closing the terminal must not
        # close the editor) via DEVNULL on all three streams.
        _subprocess.Popen(
            argv,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            stdin=_subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        # Best-effort: a failed launch must never break init / dev.
        return False

    verb = "revealed" if argv[0] == "open" else "opened"
    console.print(f"\n[green]✓[/green] {verb} [cyan]{path}[/cyan] in [bold]{label}[/bold]")
    return True


# ---------------------------------------------------------------------------
# Project mode
# ---------------------------------------------------------------------------


def _init_project(  # noqa: PLR0912 — orchestrator; per-step branches read clearer flat
    *,
    name: str | None,
    target: Path,
    force: bool,
    skip_snapshot: bool,
    with_agents: str | None = None,
    quiet: bool = False,
    open_editor: bool = True,
) -> tuple[str, Path, str | None]:
    """Bootstrap a fresh movate workspace.

    Two layouts depending on ``name``:

    * ``name`` given:   creates ``<target>/<name>/`` as the project root.
    * ``name`` blank:   bootstraps ``<target>`` itself in place.

    Either way, the resulting directory gets ``movate.yaml`` +
    ``.env.example`` + ``.gitignore`` + an empty ``agents/`` dir with
    a ``.gitkeep`` placeholder. Then we auto-snapshot — operators get
    a baseline to ``mdk diff`` / ``mdk rollback`` against from day one.

    Returns ``(project_name, project_root, snapshot_short)`` so a
    batch caller (``--with-agents``) can fold the project metadata
    into its single combined summary Panel.

    ``quiet=True`` suppresses the per-project Panel render. Used by
    the ``--with-agents`` flow which renders ONE combined Panel
    afterward covering both the project + the agents.
    """
    if name:
        project_root = (target / name).resolve()
        project_name = name
        if project_root.exists() and not force:
            err_console.print(
                f"[red]✗[/red] {project_root} already exists "
                "(use [bold]--force[/bold] to overwrite)"
            )
            raise typer.Exit(code=2)
        if project_root.exists() and force:
            shutil.rmtree(project_root)
        project_root.mkdir(parents=True)
    else:
        project_root = target.resolve()
        project_name = project_root.name
        # In-place bootstrap: refuse if there's already a project
        # marker file (project.yaml / policy.yaml / movate.yaml) unless
        # --force is set. Avoids clobbering an existing project.
        from movate.core.config import (  # noqa: PLC0415
            PROJECT_MARKER_FILES,
            is_project_root,
        )

        if is_project_root(project_root) and not force:
            existing = next(
                (f for f in PROJECT_MARKER_FILES if (project_root / f).is_file()),
                "project.yaml",
            )
            err_console.print(
                f"[red]✗[/red] {project_root}/{existing} already exists "
                "(use [bold]--force[/bold] to overwrite the project config)"
            )
            raise typer.Exit(code=2)
        project_root.mkdir(parents=True, exist_ok=True)

    # Project-level config files. Canonical name is `project.yaml`
    # (the May 2026 MVP rename). Loader still accepts `policy.yaml`
    # and `movate.yaml` for back-compat.
    (project_root / "project.yaml").write_text(_PROJECT_MOVATE_YAML.format(name=project_name))
    (project_root / ".env.example").write_text(_PROJECT_ENV_EXAMPLE)
    (project_root / ".gitignore").write_text(_PROJECT_GITIGNORE)
    # AGENTS.md — the cross-agent onboarding file (ADR 025). Teaches a
    # coding agent how to evolve THIS project: canonical layout, the
    # authoring-command catalog, the post-edit feedback loop, guardrails.
    (project_root / "AGENTS.md").write_text(_PROJECT_AGENTS_MD.format(name=project_name))

    # Four empty top-level dirs with .gitkeep placeholders so they
    # survive `git add`:
    #
    # * ``agents/``    — agent definitions (`mdk add` + `mdk init <name>`)
    # * ``skills/``    — reusable skill definitions (`skill.yaml` + impl)
    # * ``contexts/``  — reusable Markdown contexts (prepended to prompts).
    #                   Agent-LOCAL contexts at `agents/<name>/contexts/`
    #                   override these on name collision.
    # * ``kb/``        — knowledge assets for RAG / skills: PDFs, JSON
    #                   corpora, embeddings (later). The `kb-lookup`
    #                   skill's corpus lives here; `web-search`-style
    #                   skills can write cached documents here too.
    #
    # Operators don't HAVE to use any of these, but pre-creating them
    # surfaces the capabilities (vs. operators discovering them through
    # doc-reading) AND lets agent.yaml's declared `skills:` /
    # `contexts:` references resolve cleanly from day one.
    for subdir in ("agents", "skills", "contexts", "kb"):
        sub = project_root / subdir
        sub.mkdir(exist_ok=True)
        (sub / ".gitkeep").write_text("")

    # Each top-level convention dir ships a tiny README explaining what
    # goes inside, so operators who open the folder see "what does this
    # do?" answered in-place rather than having to grep the docs.
    # All three follow the same shape: What goes here, Conventions,
    # Examples that ship with templates, See also.
    (project_root / "kb" / "README.md").write_text(_KB_README)
    (project_root / "contexts" / "README.md").write_text(_CONTEXTS_README)
    (project_root / "skills" / "README.md").write_text(_SKILLS_README)

    # Bootstrap the .mdk/ runtime-state directory and land a README
    # explaining it. Operators who poke into .mdk/ wondering "what is
    # this?" find the answer in-place. The snapshot sub-command creates
    # the full tree; we create the top-level dir here so the README
    # exists even when --skip-snapshot is passed.
    mdk_state_dir = project_state_dir(project_root)
    mdk_state_dir.mkdir(exist_ok=True)
    (mdk_state_dir / "README.md").write_text(_MOVATE_STATE_README)

    # Initial snapshot — operators get a baseline for diff / rollback.
    snapshot_short: str | None = None
    if not skip_snapshot:
        try:
            from movate.snapshot import create_snapshot  # noqa: PLC0415

            manifest = create_snapshot(
                project_root=project_root,
                description="initial project scaffold",
                extras={"created_by": "mdk init --project"},
            )
            snapshot_short = manifest.hash.removeprefix("sha256:")[:8]
        except Exception as exc:
            # If the snapshot module isn't available or anything goes
            # sideways, fall back to a warning rather than rolling back
            # the entire init. The project files are still useful.
            err_console.print(f"[yellow]⚠[/yellow] initial snapshot skipped: {exc}")

    # Quiet mode: the caller (--with-agents flow) will render ONE
    # combined Panel covering both the project + the agents. Skip the
    # standalone Project Panel here to avoid double-rendering.
    if quiet:
        return project_name, project_root, snapshot_short

    body = (
        f"[bold]Project:[/bold]   [cyan]{project_name}[/cyan]\n"
        f"[bold]Path:[/bold]      [bold cyan]{project_root}[/bold cyan]   "
        f"[dim](open this folder in your IDE — agents/, skills/, contexts/, kb/ "
        f"are all here)[/dim]\n\n"
        f"  • [cyan]project.yaml[/cyan]   project config\n"
        f"  • [cyan]AGENTS.md[/cyan]      guide for AI coding agents (Claude Code, Cursor, …)\n"
        f"  • [cyan].env.example[/cyan]   env-var template\n"
        f"  • [cyan].gitignore[/cyan]     standard ignores\n"
        f"  • [cyan]agents/[/cyan]        empty (waiting for agents)\n"
        f"  • [cyan]skills/[/cyan]        empty (reusable skill defs)\n"
        f"  • [cyan]contexts/[/cyan]      empty (reusable Markdown contexts)\n"
        f"  • [cyan]kb/[/cyan]            empty (knowledge assets for RAG / skills)\n"
    )
    if snapshot_short:
        body += f"  • [cyan]snapshot[/cyan]       [dim]{snapshot_short}[/dim] (initial baseline)\n"
    # Combined cd + first-real-action line is copy-paste-friendly —
    # operators don't have to retype the project name on the second
    # line. Defaults to `mdk add --list` (browse role catalog) since
    # most operators want to see what's available before adding.
    # Tip about `.env` is deferred to a dim note — the credentials
    # store (PR #66) means most operators don't need to touch .env.
    # Two next-steps modes depending on whether `--with-agents` was
    # used. If agents are already in place, the suggested commands
    # point at the next stage (validate / run / eval). If not, the
    # suggestions point at adding agents — plus a discoverability tip
    # about `--with-agents`.
    cd_to = _cd_target(project_root)
    if with_agents:
        # Agents already added by the caller. Show forward-looking
        # commands: doctor agent / run / eval / deploy.
        agent_list = [t.strip() for t in with_agents.split(",") if t.strip()]
        first_agent = agent_list[0] if agent_list else "<agent>"
        body += (
            f"\n[bold]Next steps[/bold] "
            f"[dim](you already added {len(agent_list)} agent(s))[/dim][bold]:[/bold]\n"
            f"  [dim]$[/dim] [bold]cd {cd_to}[/bold]\n"
            f"  [dim]$[/dim] [bold]mdk doctor agent {first_agent}[/bold]"
            f"   [dim]# per-agent health check[/dim]\n"
            f"  [dim]$[/dim] [bold]mdk run {first_agent} '{{...}}'[/bold]"
            f"   [dim]# try one live[/dim]\n"
            f"  [dim]$[/dim] [bold]mdk eval {first_agent} --gate 0.7[/bold]"
            f"   [dim]# gate on the seed dataset[/dim]"
        )
    else:
        # No agents yet. Suggest `add --list` + drop the
        # `--with-agents` discoverability tip so operators see the
        # one-command alternative for next time.
        # Next-steps live in the shared interactive picker below; the
        # `--with-agents` tip + API-keys footer stay in-Panel as
        # informational sidebars (different concerns from "what
        # command should I run next?").
        body += (
            "\n[dim]Tip: skip the two-step flow next time with "
            "[bold]--with-agents[/bold]:[/dim]\n"
            "  [dim]$ mdk init <name> --with-agents rag-qa,ticket-triager[/dim]\n\n"
            "[dim]API keys: configured globally via "
            "[bold]mdk auth login <provider>[/bold] — supported providers: "
            "[bold]openai[/bold], [bold]anthropic[/bold], "
            "[bold]azure[/bold], [bold]gemini[/bold]. Per-project "
            "[bold].env[/bold] still works as an override "
            "(see [bold].env.example[/bold]).[/dim]"
        )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] Project initialized",
            title_align="left",
            border_style="green",
        )
    )

    # 'Next:' picker — the single next-step surface (renders list in
    # all modes; prompts only under TTY). The picker is the
    # canonical answer to "what do I type next?", so we don't ALSO
    # render a static `Next steps:` block inside the Panel (would
    # duplicate for interactive operators).
    from movate.cli._next_steps import NextStep, mdk_bin_name, prompt_next_step  # noqa: PLC0415

    bin_name = mdk_bin_name()
    # Pick an editor command best-effort for the post-init MENU pick (the
    # numbered "open it" action the operator can still trigger if the
    # auto-launch was skipped). Most operators on macOS/Linux have `code`
    # (VS Code); we fall back to `open` (macOS Finder) so the menu action
    # always runs even without VS Code installed.
    import shutil as _shutil  # noqa: PLC0415

    editor_cmd: str | None = None
    editor_argv: list[str] | None = None
    editor_label = ""
    if _shutil.which("code"):
        editor_cmd = f"code {project_root}"
        editor_argv = ["code", str(project_root)]
        editor_label = "Open project in VS Code"
    elif _shutil.which("cursor"):
        editor_cmd = f"cursor {project_root}"
        editor_argv = ["cursor", str(project_root)]
        editor_label = "Open project in Cursor"
    elif _shutil.which("open"):  # macOS Finder fallback
        editor_cmd = f"open {project_root}"
        editor_argv = ["open", str(project_root)]
        editor_label = "Reveal project in Finder"

    # Auto-launch via the ONE shared launcher (ADR 026 D3) — same gating as
    # the --llm / -t init paths and `mdk dev`: TTY-only, --no-open opt-out,
    # best-effort. Operators almost always want the new project open as the
    # immediate next step; on a skip / failure the menu pick below still
    # offers it manually.
    editor_auto_launched = _launch_editor(project_root, open_editor=open_editor)

    # `cd <project>` reminder. We can't change the parent shell's cwd
    # from a child process, but printing the command prominently makes
    # the next step obvious. The post-init menu shortcuts use `sh -c`
    # to chdir internally so menu picks still land in the right place.
    console.print(
        f"\n[dim]Next: [bold]cd {cd_to}[/bold] to start working in the new project.[/dim]"
    )

    next_steps = []
    # Only offer the manual editor-launch step when we DIDN'T auto-launch.
    # editor_cmd + editor_argv are set together, so guarding on argv (the
    # non-None one mypy can narrow) covers both.
    if editor_cmd is not None and editor_argv is not None and not editor_auto_launched:
        next_steps.append(NextStep(label=editor_label, command=editor_cmd, argv=editor_argv))
    # Replaces the old fixed `[3] Add FAQ` / `[4] Add rag-qa+ticket-triager`
    # shortcuts with a single dynamic picker — same numbered role table
    # the user sees in `mdk add --list`. Operators who know exactly
    # which template they want can still type `mdk add <name>` directly;
    # this menu just covers "I want to see what's available."
    next_steps.append(
        NextStep(
            label="Browse + add agents (numbered role catalog)",
            command=f"cd {cd_to} && {bin_name} add --list",
            argv=["sh", "-c", f"cd {cd_to} && {bin_name} add --list"],
        )
    )
    # `prompt_next_step` auto-renders `[s] Skip`; no need for an
    # explicit skip step. Operators who decline get a plain shell back.
    prompt_next_step(console=console, steps=next_steps)

    return project_name, project_root, snapshot_short


# ---------------------------------------------------------------------------
# Agent mode (the original behavior, preserved verbatim)
# ---------------------------------------------------------------------------


def _scaffold_with_agents(
    *,
    project_root: Path,
    agents_csv: str,
    force: bool,
    project_name: str,
    snapshot_short: str | None,
) -> None:
    """Scaffold a comma-separated list of role templates inside a
    just-created project, then render ONE combined summary Panel.

    Dispatches to the same ``_add_one`` helper that ``mdk add`` uses
    (so auto-validate, template-source marker, skill auto-scaffold are
    identical) but in QUIET mode — the per-agent Panel is suppressed
    and each call returns a dict of summary fields. After every
    template is scaffolded we render ONE combined Panel showing:

    * Project name + path + (optional) snapshot baseline hash.
    * Each agent with its role description and a ✓ / ⚠ validation
      marker.
    * Workspace-level next steps including ``mdk validate --all``.

    The greppable ``mdk_add_summary:`` lines still fire (one per
    agent) so CI parsing keeps working.
    """
    from movate.cli.add_cmd import _ROLE_DESCRIPTIONS, _add_one  # noqa: PLC0415
    from movate.templates import TEMPLATES, list_templates  # noqa: PLC0415

    templates = [t.strip() for t in agents_csv.split(",") if t.strip()]
    if not templates:
        return

    # Validate up-front so a typo in slot 3 doesn't leave slots 1 and 2
    # scaffolded behind a broken third entry. Mirrors `mdk add`.
    invalid = [t for t in templates if t not in TEMPLATES]
    if invalid:
        err_console.print(
            f"[red]✗[/red] unknown template(s): "
            f"{', '.join(repr(t) for t in invalid)}.\n"
            f"[dim]available: {', '.join(list_templates())}[/dim]"
        )
        raise typer.Exit(code=2)

    # Drop each agent under ./agents/ inside the project root.
    agents_dir = project_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    added: list[dict[str, object]] = []
    for template in templates:
        info = _add_one(
            template=template,
            agent_name=template,
            target_dir=agents_dir,
            force=force,
            project_root=project_root,
            no_validate=False,
            no_skills=False,
            quiet=True,
        )
        if info is not None:
            added.append(info)

    # Render ONE combined Panel covering the project + every agent.
    _render_combined_init_summary(
        project_name=project_name,
        project_root=project_root,
        snapshot_short=snapshot_short,
        added=added,
        role_descriptions=_ROLE_DESCRIPTIONS,
    )


def _render_combined_init_summary(
    *,
    project_name: str,
    project_root: Path,
    snapshot_short: str | None,
    added: list[dict[str, object]],
    role_descriptions: dict[str, tuple[str, str]],
) -> None:
    """Render the unified Panel for ``mdk init --project --with-agents``.

    Replaces three previous output blobs (per-agent legacy text, per-
    agent Rich Panel, end-of-batch summary Panel) with one Panel that
    summarizes the WHOLE workspace in ~12 lines: project info, agent
    table, next steps. The role descriptions (cribbed from add_cmd.py)
    give the operator a one-line sense of what each agent does
    without having to re-grep the catalog.
    """
    n_agents = len(added)

    lines = [
        f"[bold]Project:[/bold]   [cyan]{project_name}[/cyan]",
        f"[bold]Path:[/bold]      [cyan]{project_root}[/cyan]",
    ]
    if snapshot_short:
        lines.append(f"[bold]Snapshot:[/bold]  [dim]{snapshot_short}[/dim] (initial baseline)")
    lines.append("")
    lines.append(f"[bold]Agents added ({n_agents}):[/bold]")

    for info in added:
        agent_name = str(info["name"])
        template = str(info["template"])
        validates = str(info["validates"])
        # Pull the one-line role description; fall back to a generic
        # phrase if the template isn't in the catalog (custom templates
        # registered by extension packages).
        desc, _feature = role_descriptions.get(template, ("", ""))
        marker = (
            "[green]✓[/green]"
            if validates == "true"
            else "[yellow]⚠[/yellow]"
            if validates == "false"
            else "[dim]·[/dim]"
        )
        line = f"  {marker} [cyan]{agent_name}[/cyan]"
        if desc:
            line += f" [dim]— {desc}[/dim]"
        lines.append(line)

    # Workspace-level next steps. `mdk validate --all` is the natural
    # follow-up (one command to confirm every agent loads cleanly) and
    # `mdk eval --gate` is the standard CI gate.
    first_name = str(added[0]["name"]) if added else "<agent>"
    cd_to = _cd_target(project_root)
    lines.extend(
        [
            "",
            "[bold]Next steps:[/bold]",
            f"  [dim]$[/dim] [bold]cd {cd_to}[/bold]",
            "  [dim]$[/dim] [bold]mdk validate --all[/bold]"
            "   [dim]# confirm every agent loads cleanly[/dim]",
            f"  [dim]$[/dim] [bold]mdk run {first_name} '{{...}}'[/bold]"
            "   [dim]# try one live[/dim]",
            "  [dim]$[/dim] [bold]mdk ci eval --mock[/bold]"
            "   [dim]# gate every agent against its baseline[/dim]",
        ]
    )

    suffix = "s" if n_agents != 1 else ""
    console.print(
        Panel(
            "\n".join(lines),
            title=f"[green]✓[/green] Workspace ready ({n_agents} agent{suffix})",
            title_align="left",
            border_style="green",
        )
    )


def _find_project_root(from_dir: Path) -> Path | None:
    """Walk up from ``from_dir`` looking for a project root marker.

    Returns the first ancestor that contains any recognised project-root
    marker (``project.yaml``, ``policy.yaml``, ``movate.yaml``), or
    ``None`` if no marker is found. Used by
    :func:`_relocate_bundled_skills` to determine where the project-
    level ``skills/`` directory should live.
    """
    for parent in from_dir.resolve().parents:
        if any((parent / m).is_file() for m in _PROJECT_MARKERS):
            return parent
    return None


def _relocate_bundled_skills(bundled_skills_dir: Path, *, target: Path) -> None:
    """Move real skills from the agent's ``skills/`` dir to the project root.

    When a template ships skill files inside its directory (e.g.
    ``templates/calc_agent/skills/calculator/``), ``shutil.copytree``
    places them under ``<agent_dir>/skills/``. The skill loader only
    scans ``<project_root>/skills/`` — so skills inside the agent dir
    are never found.

    This function relocates every skill directory EXCEPT ``example-skill``
    (the default scaffold's reference template, which stays in-place
    intentionally) to the project-level ``skills/`` directory:

    * In **project mode** (``movate.yaml`` found ancestor): the project root
      is the ancestor containing the marker.
    * In **standalone mode** (no marker found): ``target`` itself is treated
      as the project root (consistent with :func:`_resolve_project_root`
      fallback in the loader).

    Skips skills that already exist at the destination (idempotent on
    ``--force`` re-runs).
    """
    if not bundled_skills_dir.is_dir():
        return

    real_skills = [
        d for d in bundled_skills_dir.iterdir() if d.is_dir() and d.name != "example-skill"
    ]
    if not real_skills:
        return

    # Determine destination project root.
    project_root = _find_project_root(target) or target.resolve()
    project_skills = project_root / "skills"
    project_skills.mkdir(parents=True, exist_ok=True)

    for skill_dir in real_skills:
        dest_skill = project_skills / skill_dir.name
        if dest_skill.exists():
            # Already present (e.g. shared skill used by multiple agents).
            shutil.rmtree(skill_dir)
            continue
        shutil.copytree(skill_dir, dest_skill)
        shutil.rmtree(skill_dir)


def _init_agent(
    *,
    name: str,
    template: str,
    target: Path,
    force: bool,
    quiet: bool = False,
) -> None:
    """Scaffold a single agent directory from a packaged template.

    ``quiet=True`` suppresses the legacy "scaffolded / Next steps"
    plain-text block. Used by batch callers (``mdk add`` /
    ``mdk init --with-agents``) that render their own Rich Panel
    afterward — without ``quiet`` operators see both the legacy
    output AND the new Panel for the same agent, doubling the
    vertical scroll.
    """
    try:
        template_dir = get_template_path(template)
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    dest = (target / name).resolve()
    if dest.exists() and not force:
        console.print(f"[red]error:[/red] {dest} already exists (use --force to overwrite)")
        raise typer.Exit(code=2)
    if dest.exists() and force:
        shutil.rmtree(dest)

    shutil.copytree(template_dir, dest)

    yaml_path = dest / "agent.yaml"
    contents = yaml_path.read_text().replace("__AGENT_NAME__", name)
    yaml_path.write_text(contents)

    # Move bundled skills (those NOT named "example-skill") to the
    # project-level skills/ directory so the skill loader can find
    # them. Canonical layout: <project>/skills/<name>/ — skill files
    # inside an agent dir are NOT auto-discovered by load_skill_registry.
    #
    # "example-skill" is a reference template in the default scaffold;
    # it stays inside the agent dir intentionally.
    bundled_skills_dir = dest / "skills"
    _relocate_bundled_skills(bundled_skills_dir, target=target)

    if quiet:
        return

    console.print(
        f"[green]✓[/green] scaffolded [bold]{template}[/bold] agent at [bold]{dest}[/bold]"
    )
    console.print("\nNext steps:")
    # Use `mdk` (the canonical command name) — `movate` still works as an
    # alias but mixing names in user-facing strings is confusing.
    console.print(f"  mdk validate {dest}")
    console.print(f"  mdk run {dest} --mock '{{}}'   # provide input matching schema/input.yaml")
    if (dest / "skills" / "example-skill").is_dir():
        # The default template ships a reference skill folder. Surface
        # it here so users know it exists + know where to look for the
        # pattern. Other templates may not include it; the dir-exists
        # check keeps the hint accurate.
        console.print(
            f"\n[dim]see [bold]{dest / 'skills' / 'example-skill' / 'README.md'}[/bold] "
            f"for the skill pattern (Python / HTTP / MCP backends).[/dim]"
        )


# ---------------------------------------------------------------------------
# LLM-scaffold mode (Phase 2 — generator + validation loop)
# ---------------------------------------------------------------------------


# Default model for LLM scaffolding. Cheap + reliable JSON-mode support;
# bumped via ``--llm-model`` if an operator wants a different trade-off.
# Same provider string format as ``agent.yaml: model.provider``.
_DEFAULT_LLM_MODEL = "openai/gpt-4o-mini-2024-07-18"

# Env var that pins the scaffold model without a flag (ADR 026 D6, layer 2).
# Mirrors the MDK_*/MOVATE_* env-var convention.
_SCAFFOLD_MODEL_ENV_VAR = "MDK_LLM_MODEL"


def _resolve_scaffold_model(*, llm_model: str, llm_model_explicit: bool) -> str:
    """Resolve the LLM that POWERS ``--llm`` by layered precedence (ADR 026 D6).

    Distinct from the GENERATED agent's runtime model (see
    :func:`_pick_target_model`, which is unchanged). Precedence, highest first:

      1. ``--llm-model <model>`` (per-invocation) — when explicitly passed.
      2. ``MDK_LLM_MODEL`` env var.
      3. project ``project.yaml: scaffold.model``.
      4. user ``~/.movate/config.yaml: scaffold.model`` (``mdk config set``).
      5. built-in default (:data:`_DEFAULT_LLM_MODEL`) — also the key-matched
         fallback the generated-agent model derives from (#108).

    ``llm_model_explicit`` distinguishes "operator typed ``--llm-model``" from
    "Typer filled in the default": only when the flag was explicitly given does
    layer 1 win. This mirrors ADR 022's runtime-key precedence resolution.
    Each lower layer is consulted only when the higher ones are unset, so a
    project/user default needn't be repeated on every invocation. No new auth
    surface — provider keys are still handled by BYOK / credential autoload.
    """
    import os  # noqa: PLC0415

    # 1. Explicit flag wins outright.
    if llm_model_explicit:
        return llm_model

    # 2. Env var.
    env_model = os.environ.get(_SCAFFOLD_MODEL_ENV_VAR, "").strip()
    if env_model:
        return env_model

    # 3. Project-level scaffold.model. Best-effort: a malformed/absent
    # project.yaml must never break init — fall through to the next layer.
    try:
        from movate.core.config import load_project_config  # noqa: PLC0415

        project_model = load_project_config().scaffold.model
        if project_model and project_model.strip():
            return project_model.strip()
    except Exception:
        pass

    # 4. User-level scaffold.model (`mdk config set scaffold.model …`).
    try:
        from movate.core.user_config import load_user_config  # noqa: PLC0415

        user_model = load_user_config().scaffold.model
        if user_model and user_model.strip():
            return user_model.strip()
    except Exception:
        pass

    # 5. Built-in default.
    return _DEFAULT_LLM_MODEL


# Canonical model string to write into the GENERATED agent's
# ``agent_yaml.model.provider`` for each provider whose key the operator
# actually has. An Anthropic-only user must get an `anthropic/...` agent
# (not the openai default) so the scaffolded agent runs with their key.
# Values are the LiteLLM-style strings used elsewhere in the repo
# (templates / pricing.yaml) so cost + run paths line up. Keyed by the
# provider env-var name from :data:`_PROVIDER_KEY_ENV_VARS`; checked in
# that order (first key present wins). Note: this is the model written
# INTO the generated agent — separate from the model used to DRIVE the
# scaffold call (``--llm-model`` / `_DEFAULT_LLM_MODEL`).
_PROVIDER_KEY_TO_AGENT_MODEL: dict[str, str] = {
    "OPENAI_API_KEY": "openai/gpt-4o-mini-2024-07-18",
    "ANTHROPIC_API_KEY": "anthropic/claude-haiku-4-5-20251001",
    "AZURE_OPENAI_API_KEY": "azure/gpt-4o-2024-08-06",
    "GEMINI_API_KEY": "gemini/gemini-1.5-flash",
    # LYZR has no canonical agent-model string in the templates; fall
    # through to the default rather than emit a guess.
}


# Sensible per-provider fallback target for the scaffolded agent.yaml's
# `model.fallback` (#127, PR1). Keyed by the PRIMARY model's provider prefix
# (before the `/`); value is a different-family model so a primary outage has
# somewhere to go. Mirrors the `agent_init` template's openai→anthropic
# default. A primary whose provider isn't listed falls back to the anthropic
# default (still a valid, different family for the common openai/azure case).
_FALLBACK_BY_PROVIDER: dict[str, str] = {
    "openai": "anthropic/claude-haiku-4-5-20251001",
    "azure": "anthropic/claude-haiku-4-5-20251001",
    "anthropic": "openai/gpt-4o-mini-2024-07-18",
    "gemini": "anthropic/claude-haiku-4-5-20251001",
}
_DEFAULT_FALLBACK_MODEL = "anthropic/claude-haiku-4-5-20251001"

# Operational defaults written into every scaffolded agent.yaml so a --llm
# agent matches the hand-init'd field set (`templates/agent_init/agent.yaml`).
_SCAFFOLD_DEFAULT_TIMEOUTS = {"call_ms": 30000, "total_ms": 60000}
_SCAFFOLD_DEFAULT_BUDGET = {"max_cost_usd_per_run": 0.50}


def _apply_canonical_agent_defaults(agent_yaml: dict[str, Any], *, target_model: str) -> None:
    """Fill the canonical operational fields into a generated ``agent_yaml``.

    Aligns a ``--llm`` scaffold with a hand-init'd agent
    (``templates/agent_init/agent.yaml``) by ensuring ``model.fallback``,
    ``timeouts``, ``budget``, and ``tags`` are present. Mutates ``agent_yaml``
    in place. GAP-FILL only — never clobbers a field the model already
    emitted (the RAG shape's ``tags``, an exemplar's ``budget``), so F2/F3
    shape-specific content is preserved.

    ``model.fallback`` is derived from the PRIMARY ``target_model``'s provider
    family so the fallback is a different family (a same-family fallback is
    pointless against a provider outage and trips the eval/judge family
    rules elsewhere). Skipped when the LLM already declared a fallback.
    """
    # model.fallback — only when absent. Pick a different-family target.
    model_block = agent_yaml.get("model")
    if isinstance(model_block, dict) and not model_block.get("fallback"):
        provider_prefix = target_model.split("/", 1)[0] if "/" in target_model else target_model
        fallback_model = _FALLBACK_BY_PROVIDER.get(provider_prefix, _DEFAULT_FALLBACK_MODEL)
        # Guard the degenerate case where the lookup somehow returns the
        # primary itself — drop the fallback rather than emit a useless
        # same-model entry.
        if fallback_model != target_model:
            model_block["fallback"] = [{"provider": fallback_model}]

    # timeouts / budget — gap-fill the standard caps.
    if "timeouts" not in agent_yaml:
        agent_yaml["timeouts"] = dict(_SCAFFOLD_DEFAULT_TIMEOUTS)
    if "budget" not in agent_yaml:
        agent_yaml["budget"] = dict(_SCAFFOLD_DEFAULT_BUDGET)

    # tags — ensure the key exists (empty list is the agent_init default).
    # A shape that already set tags (RAG: ["rag", "qa", "grounded"]) keeps them.
    if "tags" not in agent_yaml:
        agent_yaml["tags"] = []


def _pick_target_model(*, llm_model: str, mock: bool) -> str:
    """Choose the model string to write into the GENERATED agent.yaml.

    Precedence:

    1. If the operator explicitly passed ``--llm-model`` (i.e. it differs
       from :data:`_DEFAULT_LLM_MODEL`), honor it verbatim — they asked
       for a specific model, give them that in the generated agent too.
    2. Otherwise, map from the FIRST provider key present in the
       environment (in :data:`_PROVIDER_KEY_ENV_VARS` order) so the
       scaffolded agent runs with the key the operator actually has.
    3. Fall back to :data:`_DEFAULT_LLM_MODEL` when nothing matches —
       which is also the right answer in ``--mock`` / no-key mode (the
       synthesized/offline agent uses the openai default; the operator
       swaps it when they wire a real key).

    This is the model written INTO the generated agent. The model used to
    *drive* the scaffold LLM call is the separate ``llm_model`` value.
    """
    import os  # noqa: PLC0415

    if llm_model != _DEFAULT_LLM_MODEL:
        # Operator overrode --llm-model: use it for the generated agent.
        return llm_model
    if not mock:
        for env_var in _PROVIDER_KEY_ENV_VARS:
            if os.environ.get(env_var, "").strip():
                mapped = _PROVIDER_KEY_TO_AGENT_MODEL.get(env_var)
                if mapped:
                    return mapped
                break
    return _DEFAULT_LLM_MODEL


# Where Phase 2 stashes a failed-second-attempt's raw payload for the
# operator to inspect. Relative to the cwd at invocation time — the
# project root in the normal flow. Operators are pointed at this path
# in the error message so they don't have to grep stderr.
_DEBUG_ARTIFACT_REL = ".mdk/llm-init-failed-{name}.json"

# Preview truncation cap for the prompt body in --dry-run mode. Long
# enough that the operator sees the agent's intent; short enough that
# Rich Panel rendering stays compact.
_DRY_RUN_PROMPT_PREVIEW_CHARS = 600

# Thin-description thresholds for the soft scaffold-quality nudge. A
# description below EITHER bar (too few words OR too short overall) gets
# an advisory hint — it still proceeds. Deliberately lenient so a normal
# one-line description clears both bars and stays quiet.
_THIN_DESC_MIN_WORDS = 6
_THIN_DESC_MIN_CHARS = 25


def _init_agent_from_llm(
    *,
    name: str,
    description: str,
    llm_model: str,
    target: Path,
    force: bool,
    dry_run: bool,
    starting_template: str,
    mock: bool = False,
    no_ingest: bool = False,
    no_verify: bool = False,
) -> None:
    """Scaffold an agent from a natural-language description.

    The flow is:

    1. Build a local runtime (:func:`build_local_runtime`) so we get a
       provider configured the same way as :command:`mdk run` does.
    2. Call :func:`generate_agent_from_description` once.
    3. Write to a tempdir; run :func:`load_agent` to validate end-to-end.
    4. On validation failure: re-prompt with the error context and retry
       ONCE. On second failure: stash raw JSON to
       ``.mdk/llm-init-failed-<name>.json`` and exit 2.
    5. On success: either copy the tempdir contents to
       ``target / name`` (the normal flow) or render a Rich preview
       to stdout (``dry_run=True``).

    The retry policy lives here rather than in :mod:`movate.scaffold`
    because retry behavior is a CLI concern — the debug-artifact path,
    the ``--dry-run`` short-circuit, and the operator-facing error
    messages all depend on the CLI's context.
    """
    # Validate inputs early — guard against silently-empty descriptions.
    if not description.strip():
        err_console.print(
            "[red]✗[/red] --llm description is empty. "
            "Pass a non-empty natural-language description of the agent."
        )
        raise typer.Exit(code=2)

    # Soft nudge on a thin/vague description: a one-word or near-empty
    # description scaffolds a generic agent the operator then has to
    # rework. Warn and PROCEED — advisory only, never blocking. The
    # thresholds are deliberately lenient (a normal one-line description
    # clears both): fewer than 6 words OR under 25 non-space characters.
    stripped_desc = description.strip()
    word_count = len(stripped_desc.split())
    non_space_chars = len(stripped_desc.replace(" ", ""))
    if word_count < _THIN_DESC_MIN_WORDS or non_space_chars < _THIN_DESC_MIN_CHARS:
        err_console.print(
            "[yellow]⚠[/yellow] short description — for a better scaffold, "
            "mention the agent's inputs, outputs, and tone."
        )

    # Destination check before the LLM call — operators get the error
    # immediately, not after spending tokens.
    dest = (target / name).resolve()
    if dest.exists() and not force and not dry_run:
        err_console.print(
            f"[red]✗[/red] {dest} already exists "
            "(use [bold]--force[/bold] to overwrite, or [bold]--dry-run[/bold] "
            "to preview without writing)"
        )
        raise typer.Exit(code=2)

    # Pre-flight: without --mock we need at least one provider API key.
    # Today this crashes deep in the LLM call with a confusing stack;
    # surface it up-front with a clear pointer.
    if not mock and not _has_any_provider_key():
        err_console.print(
            "[red]✗[/red] [bold]--llm[/bold] needs a provider API key.\n"
            "[dim]Set one of: [bold]OPENAI_API_KEY[/bold], "
            "[bold]ANTHROPIC_API_KEY[/bold], [bold]AZURE_OPENAI_API_KEY[/bold], "
            "[bold]GEMINI_API_KEY[/bold] in your shell or [bold].env[/bold].\n"
            "Or re-run with [bold]--mock[/bold] for an offline scaffold "
            "(uses the deterministic mock provider, no key needed).[/dim]"
        )
        raise typer.Exit(code=2)

    import asyncio  # noqa: PLC0415

    asyncio.run(
        _run_llm_scaffold(
            name=name,
            description=description,
            llm_model=llm_model,
            target=target,
            force=force,
            dry_run=dry_run,
            starting_template=starting_template,
            mock=mock,
            no_ingest=no_ingest,
            no_verify=no_verify,
            dest=dest,
        )
    )


async def _run_llm_scaffold(
    *,
    name: str,
    description: str,
    llm_model: str,
    # `target` and `starting_template` are kept on the signature so
    # Phase 3 (UX polish + template-aware meta-prompt) can plug in
    # without churning callers. They're unused by today's body.
    target: Path,
    force: bool,
    dry_run: bool,
    starting_template: str,
    mock: bool,
    no_ingest: bool,
    no_verify: bool,
    dest: Path,
) -> None:
    """Async body of the LLM-scaffold flow.

    Split out so :func:`_init_agent_from_llm` can stay a thin sync
    Typer handler — asyncio.run owns one event loop, here.
    """
    # Local imports — keep the cold-path init flow free of these
    # heavyweight modules. The non-LLM scaffold doesn't pay this cost.
    import tempfile  # noqa: PLC0415

    from movate.cli import _console  # noqa: PLC0415
    from movate.cli._progress import spinner  # noqa: PLC0415
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.models import TokenUsage  # noqa: PLC0415
    from movate.core.scaffold_preview import (  # noqa: PLC0415
        PreviewFailureMode,
        PreviewProgressEvent,
        ScaffoldPreviewError,
        preview_agent_from_description,
    )
    from movate.scaffold import write_agent_files  # noqa: PLC0415

    # The model written INTO the generated agent (key-matched), distinct
    # from `llm_model` which DRIVES the scaffold call. See _pick_target_model.
    target_model = _pick_target_model(llm_model=llm_model, mock=mock)

    # Name the model that will run the scaffold so the operator sees what's
    # being called, and flag the offline mock path so a hung-looking spinner
    # isn't mistaken for a real (paid) provider call.
    model_label = f"{llm_model} (mock, offline)" if mock else llm_model

    rt = await build_local_runtime(mock=mock)

    # ADR 032 D1 factor-out: the actual generate+validate retry loop now lives
    # in :func:`movate.core.scaffold_preview.preview_agent_from_description`
    # — the same callable the runtime's ``POST /api/v1/agents/preview``
    # endpoint uses. Behavior here is byte-identical to the pre-refactor flow:
    # up to two attempts, the second feeding the parsed-but-invalid candidate
    # + validation error back to the model for self-correction. The
    # per-attempt spinner / between-attempts warning UX is wired through the
    # progress callback so the operator sees the same console output as before.

    # Two-stage spinner: a placeholder for attempt 1 swapped to "retrying…" if
    # attempt 2 fires. Managed via an ExitStack so the spinner cleans up on
    # the success path AND on failure.
    import contextlib  # noqa: PLC0415

    spinner_stack = contextlib.ExitStack()

    def _on_progress(event: PreviewProgressEvent, message: str | None) -> None:
        """Drive the CLI's Rich console output as the shared pipeline runs.

        Closes over ``spinner_stack`` to swap the per-attempt spinner and
        ``err_console`` to render the yellow between-attempts warning. Keeps
        the wire format byte-identical to the pre-refactor flow.
        """
        if event is PreviewProgressEvent.ATTEMPT_STARTED:
            spinner_stack.enter_context(spinner(f"scaffolding '{name}' with {model_label}…"))
        elif event is PreviewProgressEvent.ATTEMPT_RETRY_STARTED:
            # Close the attempt-1 spinner and open the attempt-2 one.
            spinner_stack.close()
            spinner_stack.enter_context(spinner(f"retrying '{name}' with {model_label}…"))
        elif event is PreviewProgressEvent.GENERATION_FAILED:
            err_console.print(
                f"[yellow]⚠[/yellow] first attempt failed: "
                f"[dim]{message}[/dim]\n"
                f"[dim]retrying once (the output was not a valid agent)...[/dim]"
            )
        elif event is PreviewProgressEvent.VALIDATION_FAILED:
            err_console.print(
                f"[yellow]⚠[/yellow] first attempt failed validation: "
                f"[dim]{message}[/dim]\n"
                f"[dim]retrying once with the error fed back to the model...[/dim]"
            )

    generated: Any = None
    total_tokens = TokenUsage()
    retried = False
    try:
        try:
            preview = await preview_agent_from_description(
                description=description,
                name=name,
                provider=rt.provider,
                model=llm_model,
                target_model=target_model,
                progress=_on_progress,
                # F3 (#112): provision the candidate's declared built-in /
                # tool-use stubbed skills into the validation tempdir's
                # project root so a RAG-shape scaffold's ``kb-vector-lookup``
                # resolves at ``load_agent`` time — identical to what the
                # committed project gets after a successful preview. The
                # runtime preview endpoint passes ``None`` (read-only; no
                # project to provision into). Lambda adapts the CLI
                # helper's kw-only ``project_root`` to the positional
                # :class:`SkillProvisioner` shape.
                skill_provisioner=lambda gen, root: _provision_declared_skills(
                    gen, project_root=root
                ),
            )
        except ScaffoldPreviewError as exc:
            # Close any open spinner before we render the failure panel.
            spinner_stack.close()
            total_tokens = exc.tokens
            retried = exc.retried
            # Preserve the pre-refactor exit-code contract:
            #   * exit 2 when the FINAL attempt failed at generation
            #     (LLMScaffoldError) — "hard scaffold failure".
            #   * exit 1 when the FINAL attempt generated but failed
            #     load-validation — "retry-validation failure".
            if exc.mode is PreviewFailureMode.GENERATION:
                _save_debug_artifact(name, payload=None, raw_error=exc.message)
                err_console.print(
                    f"[red]✗[/red] LLM scaffold failed: {exc.message}\n"
                    f"[dim]raw error saved to "
                    f"[bold]{_DEBUG_ARTIFACT_REL.format(name=name)}[/bold][/dim]"
                )
                _print_init_summary_line(
                    name=name,
                    llm=True,
                    model=llm_model,
                    tokens=total_tokens,
                    ok=False,
                    retried=retried,
                )
                raise typer.Exit(code=2) from None
            # PreviewFailureMode.VALIDATION (or the empty-description guard,
            # which the CLI front-loaded; treat anything else here as a
            # validation failure for symmetry).
            _save_debug_artifact(name, payload=exc.partial_agent, raw_error=exc.message)
            err_console.print(
                f"[red]✗[/red] scaffold failed validation after retry:\n"
                f"[dim]{exc.message}[/dim]\n"
                f"[dim]raw LLM output saved to "
                f"[bold]{_DEBUG_ARTIFACT_REL.format(name=name)}[/bold][/dim]\n"
                f"[dim]inspect, fix manually, or re-run with a different "
                f"description.[/dim]"
            )
            _print_init_summary_line(
                name=name,
                llm=True,
                model=llm_model,
                tokens=total_tokens,
                ok=False,
                retried=retried,
            )
            raise typer.Exit(code=1) from None
        else:
            spinner_stack.close()
            generated = preview.agent
            total_tokens = preview.tokens
            retried = preview.retried
    finally:
        spinner_stack.close()
        await shutdown_runtime(rt.storage, rt.tracer)

    # Compute cost. Lookups against the pricing table can fail (model
    # not listed) — that's not a hard failure for scaffold; we report
    # ``None`` and the summary line carries cost_usd= unset. The cost
    # echo Panel just omits the line.
    cost_usd = _safe_cost(model=llm_model, tokens=total_tokens)

    # At this point, ``generated`` passed validation. Either preview or
    # commit to ``dest``.
    if dry_run:
        _render_dry_run_preview(generated, name=name, dest=dest)
        _emit_post_success_hint(_console, dry_run=True)
        _print_init_summary_line(
            name=name,
            llm=True,
            model=llm_model,
            tokens=total_tokens,
            ok=True,
            retried=retried,
        )
        return

    # Commit: write into a tempdir then atomic-rename into place. The
    # tempdir-write pattern avoids leaving a half-written agent dir if
    # disk fills up mid-write (rare but easy to defend against).
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp_dir = Path(raw_tmp) / name
        write_agent_files(generated, target_dir=tmp_dir)
        if dest.exists() and force:
            # --force was set (we checked above); replace cleanly.
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmp_dir, dest)

    # F3 (#112) + F1' (#137): provision the agent's declared skills into the
    # committed project's `skills/` dir so the written agent loads + validates
    # (skill resolution + the ADR 023 retrieval.skill cross-link) — same as
    # `mdk add` does. The RAG shape declares the built-in `kb-vector-lookup`;
    # a tool-use shape declares a verb-derived skill that gets a fill-in STUB.
    # Project root = the nearest project marker above `dest` (project mode →
    # `<project>/skills/`), else `dest.parent` (`--bare` → beside the agent),
    # matching the loader's `_resolve_project_root` fallback. A QA /
    # classifier / summarizer / extraction scaffold declares no skills →
    # this is a no-op.
    scaffold_project_root = _find_project_root(dest) or dest.parent
    _provision_declared_skills(generated, project_root=scaffold_project_root)
    # Tool-use stubs the operator must implement — surfaced in the success
    # panel + next-steps hint (computed after provisioning so a reused skill
    # isn't reported as a fresh stub).
    stubbed_skills = _stubbed_skill_names(generated, project_root=scaffold_project_root)

    _render_success_panel(
        name=name,
        dest=dest,
        generated=generated,
        cost_usd=cost_usd,
        stubbed_skills=stubbed_skills,
        project_root=scaffold_project_root,
    )

    # F7 (#116): close the loop — if the description carried a URL and we
    # scaffolded a grounded (RAG) agent, auto-ingest that URL into the new
    # agent's KB so it can answer immediately. Best-effort + never breaks
    # init: the agent is already on disk and valid. Skipped entirely under
    # --mock (offline) / --no-ingest, for a non-grounding scaffold, or when
    # the description has no URL — each case prints the manual hint instead.
    ingested_pages = await _maybe_auto_ingest(
        name=name,
        description=description,
        generated=generated,
        project_root=scaffold_project_root,
        mock=mock,
        no_ingest=no_ingest,
    )

    # F8 (#117): grounded end-to-end verify — run ONE probe query through
    # the just-scaffolded RAG agent (reusing the local-run/Executor path
    # with ADR-023 auto-retrieval active) and confirm it answers FROM the
    # freshly-ingested KB. Strictly best-effort + never breaks init: the
    # agent is already on disk + valid. Skipped under --no-verify, for a
    # non-grounding scaffold, or when there's nothing to verify (no chunks
    # ingested). Under --mock it's a structural smoke (the agent RUNS
    # against MockProvider), not a real-grounding assertion.
    await _maybe_verify_grounded(
        name=name,
        generated=generated,
        project_root=scaffold_project_root,
        dest=dest,
        mock=mock,
        no_verify=no_verify,
        ingested_pages=ingested_pages,
    )

    _emit_post_success_hint(_console, dry_run=False)
    _print_init_summary_line(
        name=name,
        llm=True,
        model=llm_model,
        tokens=total_tokens,
        ok=True,
        retried=retried,
    )


def _try_validate(generated: Any, *, name: str) -> str | None:
    """Write ``generated`` to a tempdir and run :func:`load_agent`, then
    cross-check each ``sample_evals`` row against the generated schemas.

    Returns ``None`` on success, or the error string on failure. The
    string is fed back to the retry prompt so the LLM can self-correct.

    Two layers of validation:

    1. ``load_agent`` — proves the agent.yaml + prompt + schemas form a
       loadable, runnable bundle (the existing contract).
    2. ``sample_evals`` rows — each present row's ``input`` is checked
       against ``input_schema`` and ``expected`` against ``output_schema``
       (the same JSON-Schema 2020-12 validator the runtime uses). Without
       this, a non-conforming eval row silently produces a dataset that
       fails on the first ``mdk eval``. Empty ``sample_evals`` is LEGAL —
       we only validate rows that are present, never force evals to exist.
    """
    import tempfile  # noqa: PLC0415

    from movate.core.loader import AgentLoadError, load_agent  # noqa: PLC0415
    from movate.scaffold import write_agent_files  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp_agent_dir = Path(raw_tmp) / name
        try:
            write_agent_files(generated, target_dir=tmp_agent_dir)
        except (OSError, ValueError) as exc:
            return f"file write failed: {exc}"
        # F3 (#112): a RAG-shaped scaffold declares the built-in
        # `kb-vector-lookup` skill. `load_agent` resolves declared skills
        # against the project's `skills/` registry — which is empty in
        # this isolated tempdir. Provision the declared built-in skill(s)
        # alongside the agent (at the tempdir's project-root level) so
        # the load-time skill resolution + ADR 023 retrieval.skill check
        # pass exactly as they will in the committed project.
        _provision_declared_skills(generated, project_root=Path(raw_tmp))
        try:
            bundle = load_agent(tmp_agent_dir)
        except AgentLoadError as exc:
            return str(exc)
        # ADR 023 load-time checks (the same ones `mdk validate` runs):
        # retrieval.skill resolves in the agent's declared skills,
        # auto_into names a field that accepts list[string], and
        # query_from is unambiguous. `load_agent` alone doesn't run these
        # — without this, a malformed RAG shape from a real LLM would
        # only fail later at `mdk validate`. Surface it now so the retry
        # loop can self-correct.
        adr023_error = _check_adr023_retrieval(bundle)
        if adr023_error is not None:
            return adr023_error

    # Cross-check sample_evals rows against the generated I/O schemas.
    # Reuse the runtime's JSON-Schema validator (no new dep). Empty list
    # is legal — the loop simply doesn't run.
    eval_error = _validate_sample_evals(generated)
    if eval_error is not None:
        return eval_error
    return None


def _validate_sample_evals(generated: Any) -> str | None:
    """Validate each present ``sample_evals`` row against the generated
    input/output schemas.

    Returns ``None`` when every row conforms (or there are no rows), or a
    descriptive error string naming the offending row + field so the
    retry prompt can steer the model to a fix. Uses
    :class:`jsonschema.Draft202012Validator` — the same validator
    :mod:`movate.core.loader` wires for runtime I/O validation.
    """
    sample_evals = getattr(generated, "sample_evals", None) or []
    if not sample_evals:
        return None

    from jsonschema import Draft202012Validator  # noqa: PLC0415
    from jsonschema import ValidationError as JSONSchemaValidationError  # noqa: PLC0415

    try:
        input_validator = Draft202012Validator(generated.input_schema)
        output_validator = Draft202012Validator(generated.output_schema)
    except Exception as exc:
        # A malformed schema is a retry-able error, not a crash — broad
        # except so the retry loop can re-prompt the model to fix it.
        return f"sample_evals validation could not build schema validators: {exc}"

    for index, row in enumerate(sample_evals):
        if not isinstance(row, dict):
            return f"sample_evals[{index}] is not an object with 'input'/'expected' keys"
        if "input" not in row:
            return f"sample_evals[{index}] is missing the 'input' key"
        if "expected" not in row:
            return f"sample_evals[{index}] is missing the 'expected' key"
        try:
            input_validator.validate(row["input"])
        except JSONSchemaValidationError as exc:
            return f"sample_evals[{index}].input does not match input_schema: {exc.message}"
        try:
            output_validator.validate(row["expected"])
        except JSONSchemaValidationError as exc:
            return f"sample_evals[{index}].expected does not match output_schema: {exc.message}"
    return None


# Built-in skills the scaffolder may emit that have a packaged skill
# template under `src/movate/templates/`. F3 (#112): a RAG-shaped
# scaffold declares `kb-vector-lookup` (the ADR 023 retrieval skill);
# it must be provisioned alongside the agent so skill resolution +
# the retrieval.skill cross-link check resolve. Keyed by the skill name
# the agent declares; value is informational (the actual template lookup
# happens in `_scaffold_one_skill` via `SKILL_TEMPLATES`).
#
# F1' (#137) generalizes this: a TOOL-USE scaffold declares a NON-built-in
# skill (a verb-derived name like `create-ticket`). That skill won't match a
# curated template, so `_scaffold_one_skill` falls back to the `skill_init`
# echo stub — a `skill.yaml` + TODO handler the operator fills in. We
# provision EVERY declared skill that has no curated template AND looks like
# a fresh tool-use stub, so the scaffolded agent loads + validates + runs
# offline instead of failing skill resolution.
_SCAFFOLDABLE_BUILTIN_SKILLS = frozenset({"kb-vector-lookup"})

# A declared skill name we'll auto-stub must look like a valid skill slug
# (lowercase, leading letter, hyphen-separated) — the same charset
# `mdk add skill` enforces. An LLM that emits a malformed name is left to
# fail loudly at skill resolution rather than scaffolding a junk directory.
_SKILL_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")


def _declared_skills(generated: Any) -> list[str]:
    """Return the agent's declared ``skills:`` list (empty if absent).

    The generated ``agent_yaml`` is a plain dict; a non-grounding,
    non-tool-use scaffold has no ``skills`` key, so this returns ``[]``
    and the provisioning paths below are all no-ops (those shapes stay
    byte-for-byte unchanged).
    """
    skills = getattr(generated, "agent_yaml", {}).get("skills")
    if not isinstance(skills, list):
        return []
    return [s for s in skills if isinstance(s, str)]


def _stubbed_skill_names(generated: Any, *, project_root: Path) -> list[str]:
    """Declared skills that WOULD be auto-stubbed as fresh tool-use stubs.

    A declared skill is stubbed when it is NOT a curated built-in
    (:data:`_SCAFFOLDABLE_BUILTIN_SKILLS`), has a valid slug name, and
    isn't already present under ``<project_root>/skills/``. Used by the
    success panel to point the operator at the TODO they must implement —
    computed AFTER provisioning so an already-present skill (reused across
    agents) isn't reported as a new stub.

    Returns names in declaration order; empty for the RAG / non-tool-use
    shapes (no fresh stub to flag).
    """
    return [
        name
        for name in _declared_skills(generated)
        if name not in _SCAFFOLDABLE_BUILTIN_SKILLS
        and _SKILL_NAME_RE.fullmatch(name) is not None
        and (project_root / "skills" / name).is_dir()
    ]


def _provision_declared_skills(generated: Any, *, project_root: Path) -> None:
    """Scaffold declared skills into ``<project_root>/skills/``.

    Two kinds of declared skill get provisioned:

    * **Curated built-ins** (:data:`_SCAFFOLDABLE_BUILTIN_SKILLS`) — F3
      (#112): a RAG-shaped scaffold declares ``kb-vector-lookup`` (the ADR
      023 retrieval skill). ``_scaffold_one_skill`` copies its REAL packaged
      template so the retrieval.skill cross-link resolves.
    * **Tool-use stubs** (F1', #137) — a tool-use scaffold declares a
      verb-derived skill (e.g. ``create-ticket``) that has no curated
      template. ``_scaffold_one_skill`` falls back to the ``skill_init`` echo
      template: a valid ``skill.yaml`` + a TODO handler the operator fills
      in, so the agent loads + validates + ``mdk run --mock`` works.

    The skill registry is built from ``<project_root>/skills/`` — so the
    skill must physically exist there for ``load_agent`` to resolve it (both
    in the validation tempdir and in the committed project). Reuses the same
    ``_scaffold_one_skill`` code path ``mdk add`` uses, so the on-disk skill
    is identical to what an operator would get.

    Idempotent: an existing project skill is never clobbered. A malformed
    skill name (not a valid slug) is left to fail loudly at skill resolution
    rather than scaffolding a junk directory. A QA / classifier / summarizer
    / extraction scaffold declares no skills → this is a no-op.
    """
    from movate.cli.add_cmd import _scaffold_one_skill  # noqa: PLC0415

    for skill_name in _declared_skills(generated):
        is_builtin = skill_name in _SCAFFOLDABLE_BUILTIN_SKILLS
        # Skip ad-hoc names that aren't valid skill slugs (other than the
        # curated built-ins, which are always allowed). Such a name is a
        # real misconfiguration — surface it at skill resolution, not here.
        if not is_builtin and _SKILL_NAME_RE.fullmatch(skill_name) is None:
            continue
        if (project_root / "skills" / skill_name).exists():
            continue
        try:
            _scaffold_one_skill(name=skill_name, project_root=project_root)
        except Exception:
            # Best-effort: a provisioning failure surfaces downstream as
            # a skill-resolution AgentLoadError with a clear message.
            # Don't mask that with a less-specific error here.
            continue


# F7 (#116): bounded auto-ingest defaults. A fresh agent's KB only needs
# enough of the site to be useful out of the box; the operator can run a
# wider `mdk kb ingest --crawl --max-pages N` later. Keeps the post-init
# network action short + predictable. Mirrors the kb command's own
# DEFAULT_MAX_PAGES / DEFAULT_MAX_DEPTH (re-exported via kb_cmd).
_AUTO_INGEST_MAX_PAGES = 10
_AUTO_INGEST_MAX_DEPTH = 1


def _is_rag_shaped(generated: Any) -> bool:
    """True iff the generated agent is the F3 grounded/RAG shape.

    The grounded scaffold declares ``retrieval.auto_into`` (ADR 023
    pre-retrieval) — the signal the Executor uses to know it should
    auto-fill ``input.context`` from the KB. Auto-ingest (F7) only fires
    for this shape: a non-grounding agent has no KB to populate, so
    ingesting a URL into it would be meaningless. A plain dict ``agent_yaml``
    without a ``retrieval`` block → ``False`` (the non-grounding path).
    """
    agent_yaml = getattr(generated, "agent_yaml", {})
    retrieval = agent_yaml.get("retrieval") if isinstance(agent_yaml, dict) else None
    if not isinstance(retrieval, dict):
        return False
    return bool(retrieval.get("auto_into"))


async def _maybe_auto_ingest(
    *,
    name: str,
    description: str,
    generated: Any,
    project_root: Path,
    mock: bool,
    no_ingest: bool,
) -> int:
    """Auto-ingest the description's URL into the new RAG agent's KB (F7, #116).

    Returns the number of pages successfully ingested (``0`` when the ingest
    was skipped for any reason — opt-out, ``--mock``, non-grounding, no URL,
    or a best-effort failure). F8 (#117) reads this to decide whether there's
    a populated KB worth running a grounded verify probe against.

    The loop-closer for ``mdk init <name> "answer questions about <url>"
    --llm``: after the RAG agent is scaffolded, crawl the URL into its KB
    so it can actually answer. Reuses the EXISTING crawl+ingest path
    (:func:`movate.cli.kb_cmd.auto_ingest_url` → ``crawl_site`` →
    ``ingest_text``) rather than reimplementing fetch / extract / chunk.

    All short-circuits print the exact manual command + return WITHOUT
    touching the network — the scaffold is already complete:

    * ``--no-ingest`` → scaffold-only by operator request.
    * ``--mock`` (offline) → never hits the network, mirroring how the
      scaffold itself short-circuits the LLM call.
    * non-grounding scaffold → no KB to populate.
    * no URL in the description → nothing to ingest (just a manual hint).

    The ingest itself is best-effort: :func:`auto_ingest_url` raises
    :class:`AutoIngestSkippedError` (no embedding key, unreachable URL, empty
    crawl, embed failure), which is caught here and turned into a warning
    + the manual command. ``mdk init`` always exits success.
    """
    from movate.cli import _console  # noqa: PLC0415
    from movate.kb.web import first_url  # noqa: PLC0415

    url = first_url(description)

    def _manual_hint(extra: str = "") -> None:
        # Stderr-only (respects --quiet) — the success Panel already went
        # to stdout. The exact command is copy-paste-ready.
        cmd = f"mdk kb ingest {name} {url} --crawl" if url else f"mdk kb ingest {name} <url>"
        prefix = f"{extra} " if extra else ""
        _console.hint(f"[dim]{prefix}→ ingest into the KB later: [bold]{cmd}[/bold][/dim]")

    if not _is_rag_shaped(generated):
        # Non-grounding agent — no retrieval-backed KB to populate. Nothing
        # to ingest, no hint needed (a classifier has no use for one).
        return 0

    if no_ingest:
        _manual_hint("[dim]--no-ingest: skipped auto-ingest.[/dim]")
        return 0

    if url is None:
        # Grounded scaffold but no URL to seed from — point at the manual
        # ingest so the operator knows the KB starts empty.
        err_console.print(
            f"[yellow]⚠[/yellow] [bold]{name}[/bold] is grounded but its KB is empty "
            "(no URL in the description to auto-ingest from)."
        )
        _manual_hint()
        return 0

    if mock:
        # Offline path: never hit the network. Mirrors how --mock
        # short-circuits the real LLM scaffold call.
        err_console.print(
            f"[yellow]⚠[/yellow] [bold]--mock[/bold]: skipped auto-ingest of "
            f"[bold]{url}[/bold] (offline). The agent's KB starts empty."
        )
        _manual_hint()
        return 0

    from movate.cli.kb_cmd import AutoIngestSkippedError, auto_ingest_url  # noqa: PLC0415

    try:
        ingested = await auto_ingest_url(
            agent=name,
            url=url,
            project_root=project_root,
            max_pages=_AUTO_INGEST_MAX_PAGES,
            max_depth=_AUTO_INGEST_MAX_DEPTH,
            crawl=True,
        )
    except AutoIngestSkippedError as exc:
        err_console.print(f"[yellow]⚠[/yellow] auto-ingest skipped: {exc}")
        _manual_hint()
        return 0
    except Exception as exc:
        # Defensive belt: any unexpected error (an import-time issue, a
        # storage backend problem) must not turn a successful scaffold
        # into a non-zero exit. Warn + manual hint, scaffold stands.
        err_console.print(f"[yellow]⚠[/yellow] auto-ingest skipped: {exc}")
        _manual_hint()
        return 0

    page_word = "page" if ingested == 1 else "pages"
    console.print(
        f"[green]✓[/green] your agent is ready and grounded on "
        f"[bold]{ingested}[/bold] {page_word} from [bold]{url}[/bold]."
    )
    return ingested


# F8 (#117): the generic probe used when the RAG scaffold shipped no
# sample-eval question to borrow. Deliberately open-ended so it grounds
# against whatever the KB holds rather than assuming a topic.
_GENERIC_GROUNDED_PROBE = "Give a brief overview based on the available context."

# Candidate input fields, in preference order, to read the probe query
# from when the agent's `retrieval.query_from` is unset. Mirrors the
# canonical-field list `mdk validate` / the Executor use to resolve the
# default query field (see `_primary_string_input_fields`).
_PROBE_QUERY_FIELD_CANDIDATES = ("query", "question", "text", "message")


def _probe_query_field(generated: Any) -> str | None:
    """Pick the input field the verify probe should write its query into.

    Precedence:

    1. The agent's declared ``retrieval.query_from`` (authoritative — it's
       what the Executor reads to build the retrieval query).
    2. The first canonical text field (`query`/`question`/`text`/`message`)
       present in the generated input schema.
    3. ``None`` when neither resolves — the caller falls back to skipping
       the verify (we can't form a probe input we're confident about).
    """
    agent_yaml = getattr(generated, "agent_yaml", {})
    retrieval = agent_yaml.get("retrieval") if isinstance(agent_yaml, dict) else None
    if isinstance(retrieval, dict):
        query_from = retrieval.get("query_from")
        if isinstance(query_from, str) and query_from.strip():
            return query_from.strip()

    input_schema = getattr(generated, "input_schema", {})
    props = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    for candidate in _PROBE_QUERY_FIELD_CANDIDATES:
        field = props.get(candidate)
        if isinstance(field, dict) and field.get("type") == "string":
            return candidate
    return None


def _probe_query_value(generated: Any, *, field: str) -> str:
    """Build the probe query string.

    Reuse the FIRST scaffolded sample-eval's value for ``field`` when one is
    present + non-empty (a realistic question the LLM thought worth asking),
    else the generic open-ended probe. Defensive against odd sample-eval
    shapes — anything unexpected falls through to the generic probe.
    """
    sample_evals = getattr(generated, "sample_evals", None) or []
    for row in sample_evals:
        if not isinstance(row, dict):
            continue
        row_input = row.get("input")
        if not isinstance(row_input, dict):
            continue
        value = row_input.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _GENERIC_GROUNDED_PROBE


def _output_is_grounded(data: dict[str, Any]) -> bool:
    """True when a run's output indicates it answered FROM the KB.

    The F3 RAG output schema carries ``grounded: bool`` + ``citations:
    list[int]``. We treat the run as grounded when EITHER signal fires:
    ``grounded`` is truthy, OR ``citations`` is a non-empty list — a model
    that cited supporting chunks grounded its answer even if it omitted the
    boolean. Tolerant of a missing/odd shape (returns ``False``): an
    ungrounded-but-successful run is a soft warning, never a hard fail.
    """
    if data.get("grounded") is True:
        return True
    citations = data.get("citations")
    return isinstance(citations, list) and len(citations) > 0


def _verify_manual_hint(name: str) -> None:
    """Stderr-only pointer at the manual ingest + run commands.

    Printed when the verify is skipped for a recoverable reason (no KB,
    no key, an execution hiccup) so the operator knows how to populate +
    test the agent by hand. Honors ``--quiet`` via ``_console.hint``.
    """
    from movate.cli import _console  # noqa: PLC0415

    _console.hint(
        f"[dim]→ populate + try it manually: "
        f"[bold]mdk kb ingest {name} <url> --crawl[/bold] then "
        f"[bold]mdk run {name} '{{...}}'[/bold][/dim]"
    )


async def _maybe_verify_grounded(
    *,
    name: str,
    generated: Any,
    project_root: Path,
    dest: Path,
    mock: bool,
    no_verify: bool,
    ingested_pages: int,
) -> None:
    """Grounded end-to-end verify of the just-scaffolded RAG agent (F8, #117).

    The final loop-closer for ``mdk init <name> "answer questions about <url>"
    --llm``: after F7 auto-ingests the URL into the new agent's KB, run ONE
    grounded probe query THROUGH the agent (reusing the existing local-run /
    Executor path — :func:`build_local_runtime` → :meth:`Executor.execute`,
    with ADR-023 auto-retrieval active so ``input.context`` is auto-filled
    from the KB) and confirm the answer is grounded. On success prints
    ``✓ verified: agent answered grounded from <N> retrieved chunks`` — the
    user's immediate proof the end-to-end RAG works.

    Strictly best-effort, mirroring F7's never-break-init contract — the
    agent is already on disk + valid, so the verify NEVER changes the exit
    code on a successfully-scaffolded agent. It skips cleanly (and returns)
    when:

    * ``--no-verify`` → operator opted out.
    * non-grounding scaffold → nothing grounded to verify.
    * ``ingested_pages == 0`` AND NOT ``--mock`` → nothing was ingested
      (no key, ingest skipped/empty, ``--no-ingest``), so there's no KB to
      ground against; point at the manual commands.
    * ``--mock`` → no real grounding to verify; instead do a deterministic
      STRUCTURAL smoke (the agent RUNS without error against MockProvider)
      and say so — do NOT require real grounding in mock mode.

    A successful-but-ungrounded answer is a SOFT warning (not a hard fail).
    Any execution / load error is caught + turned into a warning + skip.
    """
    from movate.cli import _console  # noqa: PLC0415

    if no_verify:
        # Independent opt-out. Stay quiet beyond a one-line note (mirrors how
        # --no-ingest announces itself) — the operator asked to skip.
        _console.hint("[dim]--no-verify: skipped grounded verify.[/dim]")
        return

    if not _is_rag_shaped(generated):
        # Non-grounding agent — there's no grounded answer contract to probe.
        return

    if not mock and ingested_pages <= 0:
        # Nothing landed in the KB (no embedding key, ingest skipped, empty
        # crawl, or --no-ingest). A grounded probe would just confirm the KB
        # is empty — skip with the manual-command hint instead.
        err_console.print(
            "[yellow]⚠[/yellow] skipped grounded verify: the agent's KB is empty "
            "(nothing was auto-ingested)."
        )
        _verify_manual_hint(name)
        return

    # Resolve the input field the probe writes its query into. Without a
    # field we can't form a confident probe input → skip cleanly.
    query_field = _probe_query_field(generated)
    if query_field is None:
        err_console.print(
            "[yellow]⚠[/yellow] skipped grounded verify: couldn't resolve the "
            "agent's query input field."
        )
        _verify_manual_hint(name)
        return

    probe_query = _probe_query_value(generated, field=query_field)

    # Announce the action before running so a slow probe isn't mistaken for
    # a hang. Stderr-only (informational; never part of any --json stdout).
    err_console.print("[bold cyan]Verifying grounded answer…[/bold cyan]")

    try:
        await _run_grounded_probe(
            name=name,
            dest=dest,
            query_field=query_field,
            probe_query=probe_query,
            mock=mock,
        )
    except Exception as exc:
        # Defensive belt — ANY failure (load error, executor crash, storage
        # hiccup) must not turn a successful scaffold into a non-zero exit.
        err_console.print(f"[yellow]⚠[/yellow] grounded verify skipped: {exc}")
        _verify_manual_hint(name)


async def _run_grounded_probe(
    *,
    name: str,
    dest: Path,
    query_field: str,
    probe_query: str,
    mock: bool,
) -> None:
    """Run ONE probe query through the agent + report the grounded outcome.

    Reuses the EXACT local-run path — :func:`build_local_runtime` (so the
    provider/storage/executor are wired identically to ``mdk run``) →
    :meth:`Executor.execute` (so ADR-023 pre-retrieval auto-fills
    ``input.context`` from the KB before the prompt renders). We do NOT
    pass ``context`` in the probe input so the default ``when: if_empty``
    retrieval fires and grounds the run against the KB.

    Two modes:

    * ``--mock`` → STRUCTURAL smoke. The MockProvider can't produce a
      genuinely grounded answer, so success here means only "the agent
      RAN without error" — reported as a structural smoke, no real
      grounding asserted.
    * real → the run is checked for grounding (``output.grounded`` and/or
      non-empty ``citations``). Grounded → ``✓ verified``; successful but
      ungrounded → a soft warning; a failed run → a warning.

    Raising propagates to :func:`_maybe_verify_grounded`'s catch-all so any
    error degrades to a skip rather than failing init.
    """
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415
    from movate.core.models import RunRequest  # noqa: PLC0415

    bundle = load_agent(dest)

    rt = await build_local_runtime(mock=mock)
    if mock:
        # Make the mock emit the agent's grounded sample output so the run
        # validates against the RAG output schema (the canned mock response
        # wouldn't). This keeps the smoke a clean structural pass, NOT a
        # real-grounding assertion.
        from movate.cli.run import _configure_mock_for_bundle  # noqa: PLC0415

        _configure_mock_for_bundle(rt.provider, bundle)

    # Probe input carries ONLY the query field — ADR-023 auto-retrieval
    # fills `context` from the KB (default when: if_empty). The number of
    # retrieved chunks is read back from the persisted run's skill_calls.
    request = RunRequest(agent=bundle.spec.name, input={query_field: probe_query})
    try:
        response = await rt.executor.execute(bundle, request)
        chunks_retrieved = await _count_retrieved_chunks(rt.storage, run_id=response.run_id)
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    if mock:
        # Structural smoke only — no real grounding in mock mode.
        if response.status == "success":
            console.print(
                f"[green]✓[/green] smoke: [bold]{name}[/bold] ran end-to-end against the "
                "mock provider [dim](--mock: structural only, real grounding not verified)[/dim]."
            )
        else:
            err_console.print(
                "[yellow]⚠[/yellow] grounded verify skipped: the [bold]--mock[/bold] "
                "structural smoke run did not succeed."
            )
            _verify_manual_hint(name)
        return

    if response.status != "success":
        # A failed probe run is a soft skip — the scaffold + KB still stand.
        err_console.print(
            f"[yellow]⚠[/yellow] grounded verify skipped: the probe run did not "
            f"succeed (status={response.status})."
        )
        _verify_manual_hint(name)
        return

    if _output_is_grounded(response.data):
        chunk_word = "chunk" if chunks_retrieved == 1 else "chunks"
        console.print(
            f"[green]✓[/green] verified: [bold]{name}[/bold] answered grounded from "
            f"[bold]{chunks_retrieved}[/bold] retrieved {chunk_word}."
        )
    else:
        # Successful-but-ungrounded → soft warning, NEVER a hard fail.
        err_console.print(
            f"[yellow]⚠[/yellow] [bold]{name}[/bold] ran but its answer wasn't "
            "grounded (no citations / grounded=false). The KB may not cover the "
            "probe question — try a more specific query or ingest more pages."
        )


async def _count_retrieved_chunks(storage: Any, *, run_id: str) -> int:
    """Best-effort count of KB chunks retrieved by the probe run.

    Reads the persisted RunRecord's ``skill_calls`` (the same source
    ``mdk run --trace`` uses) and sums the ``chunks`` returned by any
    kb-flavored skill call — the ADR-023 pre-retrieval phase records a
    turn-0 skill call. Returns ``0`` on any lookup failure: the count is
    informational decoration on the success line, never load-bearing.
    """
    if not run_id:
        return 0
    try:
        record = await storage.get_run(run_id, tenant_id="local")
    except Exception:
        return 0
    if record is None or not getattr(record, "skill_calls", None):
        return 0
    total = 0
    for call in record.skill_calls:
        if "kb" not in getattr(call, "skill", "").lower():
            continue
        output = getattr(call, "output", None) or {}
        chunks = output.get("chunks") if isinstance(output, dict) else None
        if isinstance(chunks, list):
            total += len(chunks)
    return total


def _save_debug_artifact(name: str, *, payload: Any, raw_error: str) -> None:
    """Stash the failed LLM output to ``.mdk/llm-init-failed-<name>.json``."""
    artifact_path = Path(_DEBUG_ARTIFACT_REL.format(name=name))
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {"error": raw_error, "name": name}
    if payload is not None:
        # GeneratedAgent.model_dump() — dump the validated Python form.
        body["payload"] = payload.model_dump() if hasattr(payload, "model_dump") else payload
    import json as _json  # noqa: PLC0415

    artifact_path.write_text(_json.dumps(body, indent=2, default=str))


def _render_dry_run_preview(generated: Any, *, name: str, dest: Path) -> None:
    """Render the generated agent as a Rich tree to stdout (no file writes)."""
    import yaml as _yaml  # noqa: PLC0415

    # Preview the schema as YAML so the dry-run mirrors what `write_agent_files`
    # commits (#127): `schema/input.yaml` + `schema/output.yaml`, not JSON.
    body = (
        f"[bold]Agent:[/bold]   [cyan]{name}[/cyan]\n"
        f"[bold]Target:[/bold]  [dim]{dest}[/dim] [yellow](dry-run; not written)[/yellow]\n\n"
        f"[bold]agent.yaml:[/bold]\n"
        f"[dim]{_yaml.safe_dump(generated.agent_yaml, sort_keys=False).strip()}[/dim]\n\n"
        f"[bold]prompt.md:[/bold]\n"
        f"[dim]{generated.prompt_md.strip()[:_DRY_RUN_PROMPT_PREVIEW_CHARS]}"
        f"{'…' if len(generated.prompt_md) > _DRY_RUN_PROMPT_PREVIEW_CHARS else ''}[/dim]\n\n"
        f"[bold]schema/input.yaml:[/bold]\n"
        f"[dim]{_yaml.safe_dump(generated.input_schema, sort_keys=False).strip()}[/dim]\n\n"
        f"[bold]schema/output.yaml:[/bold]\n"
        f"[dim]{_yaml.safe_dump(generated.output_schema, sort_keys=False).strip()}[/dim]\n\n"
        f"[bold]evals/dataset.jsonl:[/bold] "
        f"[dim]{len(generated.sample_evals)} entries[/dim]"
    )
    console.print(
        Panel(
            body,
            title="[yellow]⌕[/yellow] LLM scaffold preview",
            title_align="left",
            border_style="yellow",
        )
    )


def _render_success_panel(
    *,
    name: str,
    dest: Path,
    generated: Any,
    cost_usd: float | None,
    stubbed_skills: list[str] | None = None,
    project_root: Path | None = None,
) -> None:
    """Print the success Panel — mirrors the template-copy success path.

    ``stubbed_skills`` (F1', #137) names any tool-use skill STUBS scaffolded
    alongside the agent; when present the panel lists each stub's location +
    a TODO line so the operator knows it's a runnable starting point they
    must finish, not a finished integration. ``project_root`` is where the
    ``skills/`` dir lives (the project root in project mode, the agent's
    parent in ``--bare``) — used to print the stub's exact path.
    """
    body = (
        f"[bold]Agent:[/bold]    [cyan]{name}[/cyan]\n"
        f"[bold]Path:[/bold]     [cyan]{dest}[/cyan]\n"
        f"[bold]Files:[/bold]\n"
        f"  • [cyan]agent.yaml[/cyan]\n"
        f"  • [cyan]prompt.md[/cyan]\n"
        f"  • [cyan]schema/input.yaml[/cyan]\n"
        f"  • [cyan]schema/output.yaml[/cyan]\n"
    )
    if generated.sample_evals:
        body += (
            f"  • [cyan]evals/dataset.jsonl[/cyan] "
            f"[dim]({len(generated.sample_evals)} seed cases)[/dim]\n"
            f"  • [cyan]evals/judge.yaml.example[/cyan] "
            f"[dim](rename to judge.yaml to enable LLM-as-judge)[/dim]\n"
        )
    # F1' (#137): tool-use skill stubs. List each stub's skill.yaml + handler
    # so the operator can find the TODO to implement.
    if stubbed_skills:
        skills_base = (project_root / "skills") if project_root is not None else Path("skills")
        body += "[bold]Skill stubs[/bold] [dim](implement the TODO):[/dim]\n"
        for skill in stubbed_skills:
            skill_dir = skills_base / skill
            body += (
                f"  • [cyan]{skill_dir / 'skill.yaml'}[/cyan]\n"
                f"  • [cyan]{skill_dir / 'impl.py'}[/cyan] "
                f"[dim](TODO: wire up the real call)[/dim]\n"
            )
    if cost_usd is not None:
        # Cost line — typical scaffold runs are <$0.01; format with
        # enough decimals to read meaningfully at that scale.
        body += f"[bold]Cost:[/bold]     [dim]${cost_usd:.6f} USD[/dim]\n"
    body += (
        f"\n[bold]Next steps:[/bold]\n"
        f"  [dim]$[/dim] [bold]mdk validate {dest}[/bold]\n"
        f"  [dim]$[/dim] [bold]mdk run {dest} --mock '{{...}}'[/bold]\n"
        f"  [dim]$[/dim] [bold]mdk eval {dest} --mock --gate 0.7[/bold]\n\n"
    )
    if stubbed_skills:
        stub_word = "stub" if len(stubbed_skills) == 1 else "stubs"
        names = ", ".join(stubbed_skills)
        body += (
            f"[dim]scaffolded by --llm · this agent uses a tool: implement the "
            f"skill {stub_word} ([bold]{names}[/bold]) before first real run.[/dim]"
        )
    else:
        body += (
            "[dim]scaffolded by --llm · review prompt.md and the schemas "
            "before first real run.[/dim]"
        )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] LLM-scaffolded agent",
            title_align="left",
            border_style="green",
        )
    )


def _accumulate_tokens(running: Any, new: Any) -> Any:
    """Sum two :class:`TokenUsage` values into a fresh instance.

    TokenUsage is a Pydantic model — addition isn't built in. This
    helper does the field-by-field sum so the running tally across
    attempt + retry adds up correctly.
    """
    from movate.core.models import TokenUsage  # noqa: PLC0415

    return TokenUsage(
        input=running.input + new.input,
        output=running.output + new.output,
        cached_input=running.cached_input + new.cached_input,
    )


def _safe_cost(*, model: str, tokens: Any) -> float | None:
    """Compute cost in USD; return ``None`` if the model isn't in the
    pricing table or the lookup fails for any other reason.

    Scaffold should never abort on a pricing-table miss — the agent
    files are already on disk and useful. We just skip the cost line.
    """
    from movate.providers.pricing import load_pricing  # noqa: PLC0415

    try:
        pricing = load_pricing()
        return pricing.cost_for(provider=model, tokens=tokens)
    except (KeyError, OSError, ValueError):
        return None


def _emit_post_success_hint(console_module: Any, *, dry_run: bool) -> None:
    """Stderr-only hint after success. Uses ``_console.hint`` so it
    respects ``--quiet`` (CI runs that pipe stderr stay clean)."""
    if dry_run:
        console_module.hint(
            "[dim]→ preview only · re-run without [bold]--dry-run[/bold] to write files[/dim]"
        )
    else:
        console_module.hint(
            "[dim]→ scaffolded by [bold]--llm[/bold] · "
            "review [bold]prompt.md[/bold] before first real run[/dim]"
        )


def _print_init_summary_line(
    *,
    name: str,
    llm: bool,
    model: str,
    tokens: Any,
    ok: bool,
    retried: bool,
) -> None:
    """Emit ``mdk_init_summary:`` greppable line.

    Mirrors :func:`movate.cli.audit_cmd._print_summary_line`,
    :func:`movate.cli.eval._print_eval_summary_line`, and
    :func:`movate.cli.doctor._print_doctor_summary_line` so CI tooling
    has one consistent prefix across all diagnostic + generation
    commands. Cost lookup happens via :func:`_safe_cost` — a missing
    pricing entry renders as ``cost_usd=unknown`` rather than failing
    the summary line altogether.
    """
    cost = _safe_cost(model=model, tokens=tokens)
    cost_str = f"{cost:.6f}" if cost is not None else "unknown"
    console.print(
        f"[dim]mdk_init_summary: "
        f"name={name} "
        f"llm={str(llm).lower()} "
        f"model={model} "
        f"input_tokens={tokens.input} "
        f"output_tokens={tokens.output} "
        f"cost_usd={cost_str} "
        f"retried={str(retried).lower()} "
        f"ok={str(ok).lower()}[/dim]"
    )


# ---------------------------------------------------------------------------
# ADR 026 D1 — context-aware agent layout + D4 exact next-steps
# ---------------------------------------------------------------------------


@dataclass
class _AgentLayout:
    """Where an agent-intent ``mdk init`` lands on disk (ADR 026 D1).

    Resolved by :func:`_resolve_agent_layout` from the (name, target, bare,
    in-project) context. The agent-scaffold paths (``-t`` / ``--llm``) read
    this to decide their write target and whether to wrap a fresh project.

    Fields:

    * ``agent_parent`` — the directory the agent dir is created UNDER (so the
      agent lands at ``agent_parent / name``). For a project layout this is
      ``<project>/agents``; for ``--bare`` it's the raw ``target``.
    * ``agent_dir`` — the resolved agent directory (``agent_parent / name``).
    * ``project_root`` — the project the agent belongs to, or ``None`` for a
      ``--bare`` standalone agent.
    * ``created_project`` — True when this invocation bootstrapped a NEW
      project to hold the agent (outside-a-project, non-bare). Drives the
      success/next-steps rendering + the post-create editor launch.
    * ``snapshot_short`` — short hash of the initial snapshot when a project
      was created (else None).
    """

    agent_parent: Path
    agent_dir: Path
    project_root: Path | None
    created_project: bool
    snapshot_short: str | None = None


def _resolve_agent_layout(
    *,
    name: str,
    target: Path,
    bare: bool,
    force: bool,
    skip_snapshot: bool,
    open_editor: bool,
) -> _AgentLayout:
    """Decide where an agent-intent ``mdk init`` writes (ADR 026 D1).

    Three context-aware layouts:

    1. ``--bare`` → STANDALONE single-dir agent at ``target/<name>/`` (the
       pre-ADR-026 output). No project wrapper. The documented escape hatch.
    2. INSIDE a project (``project.yaml`` up the tree from cwd) → ADD the
       agent under ``<project>/agents/<name>/`` (like ``mdk add``). No nested
       project.
    3. OUTSIDE a project, not bare → bootstrap a PROJECT at ``target/<name>/``
       (project.yaml + AGENTS.md + .env.example + .gitignore + initial
       snapshot, reusing :func:`_init_project`) and put the agent under
       ``<project>/agents/<name>/`` so ``mdk run <name>`` works from the root.

    The project bootstrap (case 3) runs in ``quiet=True`` mode so the
    agent-scaffold path renders ONE combined success surface afterward
    rather than two stacked panels.
    """
    if bare:
        # Legacy standalone agent — no project, agent lands at target/<name>.
        return _AgentLayout(
            agent_parent=target,
            agent_dir=(target / name).resolve(),
            project_root=None,
            created_project=False,
        )

    existing_root = _enclosing_project_root()
    if existing_root is not None:
        # In a project → add the agent under <project>/agents/.
        agents_dir = existing_root / "agents"
        return _AgentLayout(
            agent_parent=agents_dir,
            agent_dir=(agents_dir / name).resolve(),
            project_root=existing_root,
            created_project=False,
        )

    # Outside a project → bootstrap one to hold the agent, then place the
    # agent under <project>/agents/. The editor launch is DEFERRED to after
    # the agent is scaffolded (open_editor handled by the caller) so the
    # operator opens a project that already contains the agent.
    _project_name, project_root, snapshot_short = _init_project(
        name=name,
        target=target,
        force=force,
        skip_snapshot=skip_snapshot,
        with_agents=None,
        quiet=True,
        open_editor=False,
    )
    agents_dir = project_root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    return _AgentLayout(
        agent_parent=agents_dir,
        agent_dir=(agents_dir / name).resolve(),
        project_root=project_root,
        created_project=True,
        snapshot_short=snapshot_short,
    )


def _enclosing_project_root() -> Path | None:
    """Project root at or above cwd, or ``None`` outside any project.

    Thin wrapper over :func:`movate.cli._resolve.walk_up_for_project_root`
    so the init dispatch reads in terms of "am I in a project?" — the same
    upward marker search the loader + ``mdk add`` use.
    """
    from movate.cli._resolve import walk_up_for_project_root  # noqa: PLC0415

    return walk_up_for_project_root()


def _run_input_example(agent_dir: Path) -> str:
    """Build a copy-pasteable run-input snippet from the agent's dataset.

    Reads the first row of ``evals/dataset.jsonl`` (the same source
    ``mdk run`` suggests on a missing-input error) so the D4 next-steps
    command is runnable first try. Falls back to ``'{…}'`` when no dataset
    sample is available — best-effort, never raises.
    """
    try:
        from movate.core.loader import load_agent  # noqa: PLC0415

        bundle = load_agent(agent_dir)
        dataset = bundle.spec.evals.dataset
        if not dataset:
            return "{…}"
        dataset_path = (bundle.agent_dir / dataset).resolve()
        if dataset_path.is_file():
            text = dataset_path.read_text().strip()
            if text:
                import json as _json  # noqa: PLC0415

                row = _json.loads(text.splitlines()[0])
                if isinstance(row, dict) and isinstance(row.get("input"), dict):
                    return _json.dumps(row["input"], separators=(", ", ": "))
    except Exception:
        pass
    return "{…}"


def _render_agent_next_steps(layout: _AgentLayout, *, name: str) -> None:
    """Print the EXACT runnable command for what landed on disk (ADR 026 D4).

    Built from the real on-disk ``_AgentLayout`` so copy-paste works first
    try, regardless of which D1 layout produced the agent:

    * created a project → ``cd <project> && mdk run <name> '<input>'``
    * added to a project → ``mdk run <name> '<input>'`` (already at/with root)
    * ``--bare`` standalone → ``cd <dir> && mdk run . '<input>'`` (ADR 026
      makes the standalone agent first-class via ``mdk run .``).

    The ``<input>`` is a real dataset sample when one exists. Rendered to
    stdout as a compact panel beneath the scaffold's own success output.
    """
    bin_name = "mdk"
    example = _run_input_example(layout.agent_dir)
    lines: list[str] = []

    if layout.created_project and layout.project_root is not None:
        cd_to = _cd_target(layout.project_root)
        lines.append(f"  [dim]$[/dim] [bold]cd {cd_to}[/bold]")
        lines.append(
            f"  [dim]$[/dim] [bold]{bin_name} run {name} '{example}'[/bold]"
            "   [dim]# run it (mock-free; add --mock for offline)[/dim]"
        )
        lines.append(
            f"  [dim]$[/dim] [bold]{bin_name} validate {name}[/bold]   [dim]# static-check it[/dim]"
        )
    elif layout.project_root is not None:
        # Added to the existing project — resolve by name from the root.
        cd_to = _cd_target(layout.project_root)
        same_dir = layout.project_root.resolve() == Path.cwd().resolve()
        prefix = "" if same_dir else f"cd {cd_to} && "
        lines.append(
            f"  [dim]$[/dim] [bold]{prefix}{bin_name} run {name} '{example}'[/bold]"
            "   [dim]# run it[/dim]"
        )
        lines.append(
            f"  [dim]$[/dim] [bold]{prefix}{bin_name} validate {name}[/bold]"
            "   [dim]# static-check it[/dim]"
        )
    else:
        # --bare standalone agent → mdk run . from inside the dir.
        cd_to = _cd_target(layout.agent_dir)
        lines.append(f"  [dim]$[/dim] [bold]cd {cd_to}[/bold]")
        lines.append(
            f"  [dim]$[/dim] [bold]{bin_name} run . '{example}'[/bold]"
            "   [dim]# standalone agent — run it by path[/dim]"
        )
        lines.append(
            f"  [dim]$[/dim] [bold]{bin_name} validate .[/bold]   [dim]# static-check it[/dim]"
        )

    console.print(
        Panel(
            "\n".join(lines),
            title="[green]✓[/green] Next steps",
            title_align="left",
            border_style="green",
        )
    )


def _provision_template_skills_and_contexts(
    *,
    agent_dir: Path,
    template: str,
    project_root: Path | None,
) -> None:
    """Auto-scaffold a template's declared skills + contexts for the
    ``mdk init -t`` path (parity with ``mdk add``).

    :func:`_init_agent` already relocates skills BUNDLED inside the template
    dir (e.g. calc-agent's ``calculator``) to ``<project>/skills/``. But
    several templates DECLARE a skill they don't bundle (rag-qa →
    ``kb-vector-lookup`` / ``web-search``; ticket-triager → ``kb-lookup``;
    code-reviewer → ``lint-runner``; …). Those are auto-scaffolded only by
    the ``mdk add`` code path (``_add_one``), so a plain ``mdk init -t rag-qa``
    used to produce an agent that fails to LOAD ("references skill X but no
    such skill is registered"). This brings ``mdk init -t`` to parity by
    reusing the exact same ``mdk add`` helpers.

    Best-effort + project-scoped: skills/contexts live at the PROJECT level,
    so this no-ops for ``--bare`` (``project_root is None``) — a standalone
    single-dir agent has no project ``skills/`` to populate. The underlying
    helpers each swallow + warn on their own failures, so this never breaks
    the scaffold.
    """
    if project_root is None:
        # --bare standalone agent: no project skills/contexts dir to populate.
        return

    from movate.cli.add_cmd import (  # noqa: PLC0415
        _maybe_copy_template_contexts,
        _maybe_scaffold_declared_contexts,
        _maybe_scaffold_declared_skills,
    )

    _maybe_scaffold_declared_skills(agent_dir=agent_dir, project_root=project_root)

    try:
        template_src_dir: Path | None = get_template_path(template)
    except ValueError:
        template_src_dir = None
    if template_src_dir is not None:
        _maybe_copy_template_contexts(template_dir=template_src_dir, project_root=project_root)
    _maybe_scaffold_declared_contexts(agent_dir=agent_dir, project_root=project_root)


def _maybe_eval_baseline(agent_dir: Path, *, mock: bool, skip: bool) -> None:
    """Post-scaffold ``--mock`` eval BASELINE for the just-scaffolded agent (F4', #138).

    Upgrades the F4 single-run smoke into an eval baseline: after a template
    or ``--llm`` scaffold lands on disk, run the new agent's eval dataset
    (``evals/dataset.jsonl``) under the deterministic mock provider and print
    a one-line baseline pass-rate (e.g. ``baseline: 5/5 cases pass under
    --mock``). This gives the operator a known-good starting point AND catches
    a template whose evals don't even run.

    Reuses the SHIPPED eval engine + mock path — it mirrors how
    ``mdk eval --mock`` wires things up (:func:`build_local_runtime` →
    :func:`_configure_mock_for_bundle` → :class:`EvalEngine`), so the baseline
    scores the exact same way the operator's next ``mdk eval --mock`` will.
    No new eval engine, no real model calls, no network.

    Gating mirrors the F8 grounded verify's never-break-init contract:

    * ``skip`` (``--no-baseline``) → operator opted out; stay quiet.
    * NOT ``mock`` → the baseline is a hermetic ``--mock`` step only. A
      real-provider scaffold would spend tokens on every ``init`` — skip with
      a one-line hint pointing at the manual command.
    * The agent can't load / has no usable dataset / the run errors → degrade
      to a WARNING, never a non-zero exit on a successfully-scaffolded agent.

    A successful baseline with a 0% pass-rate is still surfaced (not a
    failure) — a freshly-scaffolded template SHOULD pass under the
    dataset-aware mock, so a low rate is a useful signal, not a crash.
    """
    from movate.cli import _console  # noqa: PLC0415

    if skip:
        _console.hint("[dim]--no-baseline: skipped the post-scaffold eval baseline.[/dim]")
        return

    if not mock:
        # The baseline is a zero-cost hermetic step. Outside --mock it would
        # call the real provider for every case on every init — too costly to
        # do silently. Point at the manual command instead.
        _console.hint(
            "[dim]eval baseline skipped (runs only under [bold]--mock[/bold]); "
            "measure anytime with [bold]mdk eval --mock[/bold].[/dim]"
        )
        return

    import asyncio  # noqa: PLC0415

    try:
        asyncio.run(_run_eval_baseline(agent_dir))
    except Exception as exc:
        # Defensive belt — ANY failure (load error, executor crash, storage
        # hiccup, missing/empty dataset) must NOT turn a successful scaffold
        # into a non-zero exit. Degrade to a warning + manual hint.
        err_console.print(
            f"[yellow]⚠[/yellow] eval baseline skipped: {exc} "
            "[dim](scaffold is intact; run [bold]mdk eval --mock[/bold] to measure)[/dim]"
        )


async def _run_eval_baseline(agent_dir: Path) -> None:
    """Run the scaffolded agent's eval dataset under ``--mock`` + report the
    baseline pass-rate. The mechanics mirror ``movate.cli.eval._run_eval``'s
    ``--mock`` branch exactly (engine, dataset-aware mock, runtime teardown).

    Raises on any failure so :func:`_maybe_eval_baseline`'s catch-all degrades
    it to a warning — the scaffold is already on disk and useful regardless.
    """
    from movate.cli._runtime import build_local_runtime, shutdown_runtime  # noqa: PLC0415
    from movate.cli.run import _configure_mock_for_bundle  # noqa: PLC0415
    from movate.core.eval import EvalEngine  # noqa: PLC0415
    from movate.core.loader import load_agent  # noqa: PLC0415

    bundle = load_agent(agent_dir)

    rt = await build_local_runtime(mock=True)
    # Dataset-aware mock (same as `mdk eval --mock`): make the MockProvider
    # cycle through the dataset's ``expected`` outputs so each case is scored
    # against the exactly-right answer instead of a single canned response.
    _configure_mock_for_bundle(rt.provider, bundle)
    try:
        engine = EvalEngine(executor=rt.executor, provider=rt.provider)
        summary = await engine.run(bundle)
        # Persist the EvalRecord so the operator's first `mdk eval --compare`
        # / drift check has a baseline to diff against — same as `mdk eval`.
        with contextlib.suppress(Exception):
            await rt.storage.save_eval(summary.to_record())
    finally:
        await shutdown_runtime(rt.storage, rt.tracer)

    passing = sum(1 for c in summary.cases if c.passed)
    total = summary.sample_count
    rate = summary.pass_rate
    if total == 0:
        # A scaffolded agent with an empty dataset — surface as a soft note,
        # not a crash. (Shipped templates always have cases; this guards a
        # hand-edited / future template.)
        err_console.print(
            "[yellow]⚠[/yellow] eval baseline: dataset has no cases — nothing to measure."
        )
        return

    marker = "[green]✓[/green]" if passing == total else "[yellow]•[/yellow]"
    console.print(
        f"{marker} baseline: [bold]{passing}/{total}[/bold] cases pass under "
        f"[bold]--mock[/bold] [dim](pass_rate={rate:.0%})[/dim]"
    )
    # Greppable summary line — mirrors mdk_init_summary / mdk_eval_summary so
    # CI tooling can assert the baseline ran with one consistent prefix.
    console.print(
        f"[dim]mdk_baseline_summary: "
        f"agent={summary.agent} "
        f"cases={total} "
        f"passing={passing} "
        f"pass_rate={rate:.3f} "
        f"mock=true[/dim]"
    )


def _finish_agent_init(
    layout: _AgentLayout,
    *,
    name: str,
    open_editor: bool,
    mock: bool,
    no_baseline: bool = False,
) -> None:
    """Common post-scaffold tail for the agent-intent ``mdk init`` paths.

    Renders the D4 exact-command next-steps panel, runs the post-scaffold
    ``--mock`` eval baseline (F4', #138 — best-effort, gated by
    ``--no-baseline`` + ``--mock``), then (best-effort, TTY-gated, --no-open /
    --mock aware via :func:`_launch_editor`) opens the right surface: the
    PROJECT root when one was created / joined, or the standalone agent dir
    for ``--bare``. The editor launch is deferred to here (not inside the
    project bootstrap) so the operator opens a workspace that already
    contains the freshly-scaffolded agent.
    """
    _render_agent_next_steps(layout, name=name)
    # Post-scaffold eval baseline (F4'). Runs against the on-disk agent in its
    # resolved layout (project or --bare). Best-effort: never changes the
    # exit code of a successfully-scaffolded agent.
    _maybe_eval_baseline(layout.agent_dir, mock=mock, skip=no_baseline)
    open_path = layout.project_root if layout.project_root is not None else layout.agent_dir
    # Only auto-launch on a freshly-created project or a bare standalone
    # agent; adding to an EXISTING project shouldn't re-open the editor
    # (the operator is presumably already working in it).
    if layout.created_project or layout.project_root is None:
        _launch_editor(open_path, open_editor=open_editor, mock=mock)


# ---------------------------------------------------------------------------
# Entry point — dispatches between project + agent modes
# ---------------------------------------------------------------------------


def init(  # noqa: PLR0912 — front-door dispatcher; mode branches read clearer flat
    name: str = typer.Argument(
        None,
        help=(
            "Name for the project (outside a project) or the agent (inside a "
            "project, or with [bold]-t[/bold]/[bold]--llm[/bold]/[bold]--bare[/bold]). "
            "Lowercase, hyphenated. Omit with [bold]--project[/bold] to bootstrap "
            "the current directory in place."
        ),
    ),
    description: str = typer.Argument(
        None,
        help=(
            "Optional natural-language description. When set, treated as "
            "shorthand for [bold]--llm[/bold] (LLM-generates the agent): "
            '[bold]mdk init faq-agent "FAQ agent for our SaaS pricing"[/bold].'
        ),
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help=(
            "Explicitly bootstrap just the project workspace (back-compat flag — "
            "outside a project this is already the default). Creates "
            "[bold]project.yaml[/bold] + [bold]AGENTS.md[/bold] + "
            "[bold].env.example[/bold] + [bold].gitignore[/bold] + an empty "
            "[bold]agents/[/bold] + an initial snapshot."
        ),
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        "-t",
        help=(
            f"Template to scaffold the agent from. One of: {', '.join(list_templates())}. "
            "Setting it scaffolds an agent (under [bold]agents/<name>/[/bold] in the "
            "project, or standalone with [bold]--bare[/bold]). Bare "
            "[bold]mdk init <name>[/bold] (no [bold]-t[/bold], no [bold]--llm[/bold]) "
            "outside a project just bootstraps the project with an empty "
            "[bold]agents/[/bold]."
        ),
    ),
    target: Path = typer.Option(
        Path("."),
        "--target",
        "--at",
        help=(
            "Where to scaffold the new agent / project. PARENT directory: the "
            "agent or project ends up at [bold]<target>/<name>/[/bold]. Accepts "
            "absolute paths ([dim]~/projects[/dim], [dim]/abs/path[/dim]) and "
            "the [bold]--at[/bold] alias which reads more naturally for "
            "[bold]--project[/bold] (e.g. [dim]mdk init --project foo --at ~/work[/dim])."
        ),
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing directory."),
    skip_snapshot: bool = typer.Option(
        False,
        "--skip-snapshot",
        help=(
            "Skip creating the initial snapshot in [bold]--project[/bold] mode. "
            "Mostly for tests; production use should keep the baseline."
        ),
    ),
    with_agents: str = typer.Option(
        None,
        "--with-agents",
        help=(
            "Comma-separated role templates to scaffold immediately after "
            "the project is created. Only meaningful with [bold]--project[/bold]. "
            "Example: [bold]--with-agents rag-qa,ticket-triager,code-reviewer[/bold] "
            "bootstraps a support workspace in one command."
        ),
    ),
    llm: str = typer.Option(
        None,
        "--llm",
        help=(
            "Natural-language description of the agent. The CLI uses an LLM "
            "to generate [bold]agent.yaml[/bold] + [bold]prompt.md[/bold] + "
            "schemas + seed eval cases. Validates by loading the result back; "
            "retries once on failure. Pair with [bold]--mock[/bold] for "
            "hermetic CI."
        ),
    ),
    llm_model: str = typer.Option(
        _DEFAULT_LLM_MODEL,
        "--llm-model",
        help=(
            f"Model to use when [bold]--llm[/bold] is set. Defaults to "
            f"[bold]{_DEFAULT_LLM_MODEL}[/bold] (cheap, reliable JSON output)."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Still calls the model (needs a provider key) and previews the "
            "generated agent without writing any files. For offline previews "
            "with no key, add [bold]--mock[/bold]. Only meaningful with "
            "[bold]--llm[/bold]; ignored otherwise."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help=(
            "Offline path: uses the deterministic mock provider (no API key) "
            "to write a generic scaffold — no real model is called. For "
            "hermetic CI. Only meaningful with [bold]--llm[/bold]; ignored "
            "otherwise."
        ),
    ),
    no_ingest: bool = typer.Option(
        False,
        "--no-ingest",
        help=(
            "Skip the auto-ingest step. By default, when an [bold]--llm[/bold] "
            "description contains a URL and scaffolds a grounded (RAG) agent, "
            "[bold]mdk init[/bold] crawls that URL into the new agent's KB so "
            "it can answer immediately. Pass [bold]--no-ingest[/bold] for a "
            "scaffold-only run (you can ingest later with "
            "[bold]mdk kb ingest[/bold]). Auto-ingest is always skipped under "
            "[bold]--mock[/bold] / [bold]--dry-run[/bold] (no network)."
        ),
    ),
    no_verify: bool = typer.Option(
        False,
        "--no-verify",
        help=(
            "Skip the post-scaffold grounded verify (F8). By default, after an "
            "[bold]--llm[/bold] description scaffolds a grounded (RAG) agent AND "
            "auto-ingest populates its KB, [bold]mdk init[/bold] runs ONE probe "
            "query through the new agent to confirm it answers grounded from the "
            "KB. The verify is best-effort + never changes the exit code on a "
            "successfully-scaffolded agent. It's skipped automatically when there "
            "is nothing to verify (non-grounding scaffold, [bold]--no-ingest[/bold], "
            "or an empty KB); under [bold]--mock[/bold] it runs a structural smoke "
            "only (the agent executes against the mock provider, no real grounding "
            "is asserted). Independent of [bold]--no-ingest[/bold]."
        ),
    ),
    no_baseline: bool = typer.Option(
        False,
        "--no-baseline",
        help=(
            "Skip the post-scaffold eval baseline (F4'). By default, after a "
            "template or [bold]--llm[/bold] scaffold AND when run under "
            "[bold]--mock[/bold], [bold]mdk init[/bold] runs the new agent's eval "
            "dataset through the deterministic mock provider and prints a one-line "
            "baseline pass-rate (e.g. [dim]baseline: 5/5 cases pass under "
            "--mock[/dim]) — a known-good starting point that also catches a "
            "template whose evals don't run. Best-effort + never changes the exit "
            "code on a successfully-scaffolded agent. It runs only under "
            "[bold]--mock[/bold] (a hermetic, zero-cost step); without "
            "[bold]--mock[/bold] measure anytime with [bold]mdk eval --mock[/bold]."
        ),
    ),
    open_editor: bool = typer.Option(
        True,
        "--open-editor/--no-open-editor",
        help=(
            "After project mode creates the workspace, launch VS Code "
            "(or Cursor, or open the folder in Finder on macOS) on the "
            "new project. Default ON when an editor is on PATH and "
            "stdout is a tty; pass [bold]--no-open-editor[/bold] in CI / "
            "headless environments. The same option is offered as a "
            "menu pick afterwards so the operator can still launch it "
            "manually if auto-launch was skipped."
        ),
    ),
    bare: bool = typer.Option(
        False,
        "--bare",
        help=(
            "Escape hatch (ADR 026 D1): scaffold a STANDALONE single-dir agent "
            "at [bold]<target>/<name>/[/bold] — no [bold]project.yaml[/bold], no "
            "[bold]agents/[/bold] wrapper. The pre-ADR-026 [bold]-t[/bold] / "
            "[bold]--llm[/bold] output. Use it to drop an agent into a non-mdk "
            "repo or for a quick throwaway experiment; run it with "
            "[bold]mdk run .[/bold] from inside the dir. Without [bold]--bare[/bold], "
            "[bold]mdk init <name> -t/--llm[/bold] yields a runnable PROJECT "
            "(or adds to the current one when inside a project)."
        ),
    ),
) -> None:
    """Scaffold a new agent or project — always leaving a runnable result.

    [bold]mdk init[/bold] is context-aware (ADR 026). The DEFAULT does the
    intuitive thing for where you are:

      [bold]Outside a project[/bold] → bootstrap a runnable PROJECT
        (project.yaml + AGENTS.md + .env.example + .gitignore + agents/ +
        an initial snapshot baseline). Add [bold]-t[/bold] / [bold]--llm[/bold]
        / a description and the agent lands under [bold]agents/<name>/[/bold]
        so [bold]mdk run <name>[/bold] works from the project root.
      [bold]Inside a project[/bold] → ADD the agent under
        [bold]agents/<name>/[/bold] (same as [bold]mdk add[/bold]).
      [bold]--bare[/bold] → a STANDALONE single-dir agent (no project) — run
        it with [bold]mdk run .[/bold] from inside the folder.

    [bold]--llm "<description>"[/bold] LLM-generates the agent: a shape-aware
    (Q&A / classifier / extractor / RAG / tool-use) [bold]agent.yaml[/bold] +
    [bold]prompt.md[/bold] + schemas + seed eval cases. If the description
    contains a URL it auto-crawls + ingests that page into the agent's KB
    then runs a grounded verify; a tool-use intent scaffolds a runnable skill
    STUB; under [bold]--mock[/bold] it also runs a post-scaffold eval baseline.
    Set a persistent scaffolder model with
    [bold]mdk config set scaffold.model <model>[/bold].

    [bold]Available agent templates:[/bold]

      [bold]default[/bold]    — minimal echo agent (string-in, string-out)
      [bold]faq[/bold]        — question → answer + confidence
      [bold]summarizer[/bold] — text + max_words → summary + word_count
      [bold]classifier[/bold] — text + labels → chosen label
      [bold]chatbot[/bold]    — message → reply (designed for `mdk chat`)
      [bold]extractor[/bold]  — text → strict typed fields
      [dim](more role templates available — see `mdk add --list`)[/dim]

    [bold]Examples:[/bold]

      [dim]$ mdk init my-proj                          # new project (empty agents/)[/dim]
      [dim]$ mdk init faq-agent -t faq                 # new project + the faq agent[/dim]
      [dim]$ mdk init faq-agent --llm "FAQ bot for our SaaS pricing"  # LLM-generated[/dim]
      [dim]$ mdk init sitebot "answer questions about https://example.com"  # crawl + ground[/dim]
      [dim]$ mdk init triager -t ticket-triager        # (run inside a project → adds it)[/dim]
      [dim]$ mdk init scratch --bare                   # standalone agent (mdk run .)[/dim]
      [dim]$ mdk init --project                        # bootstrap current dir in place[/dim]

    [bold]See also:[/bold]
      [bold]mdk dev <name>[/bold] — the guided front door: scaffolds (if
        needed) then drops you into a live edit → test → deploy loop.
      [bold]mdk run <name>[/bold] / [bold]validate <name>[/bold] — resolve an
        agent by name from the project root; use [bold]mdk run .[/bold] for a
        standalone agent.
      [bold]mdk add <template>[/bold] — drop another role agent into this
        project ([bold]mdk add --list[/bold] for the catalog).
      [bold]mdk kb ingest <agent> <url|path>[/bold] — populate a grounded
        agent's KB (e.g. when [bold]--llm[/bold] had no URL to crawl).
      [bold]mdk report[/bold] — offline rollup of how your agents are doing.
      [bold]mdk authoring audit[/bold] / [bold]replay[/bold] — the copilot's
        reversible-action audit log.
    """
    # Mutual-exclusion guard: --llm only makes sense in agent mode.
    # Project mode is just a movate.yaml + .gitignore + empty agents/ —
    # nothing for an LLM to scaffold. Point the operator at agent mode
    # so they don't have to read the long --help to figure it out.
    if project and llm is not None:
        err_console.print(
            "[red]✗[/red] [bold]--llm[/bold] is for agent scaffolding, not "
            "project bootstrap.\n"
            "[dim]Run [bold]mdk init --project <name>[/bold] first to create "
            "the workspace, then\n"
            '[bold]mdk init <agent-name> --llm "<description>"[/bold] '
            "inside it.[/dim]"
        )
        raise typer.Exit(code=2)

    # Default dispatch (May 2026): bare `mdk init <name>` (no `-t`, no
    # `--llm`, no positional description) scaffolds a PROJECT, not an
    # agent. Rationale: "init = project, add = agent" matches the
    # operator mental model. Agent-mode is still reachable via:
    #
    #   mdk init <name> -t <template>           (template-scaffold)
    #   mdk init <name> --llm "<description>"   (LLM-scaffold)
    #   mdk init <name> "<description>"         (positional shorthand)
    #
    # The legacy `--project` flag still works (back-compat) but is no
    # longer required when the operator wants project mode.
    has_template = template is not None
    has_llm_intent = llm is not None or description is not None
    implicit_project_mode = name is not None and not has_template and not has_llm_intent

    if project or (implicit_project_mode and not with_agents) or with_agents:
        # If we're scaffolding a project INSIDE an existing project,
        # the operator probably meant `mdk add` (which scaffolds an
        # agent into the current project) rather than nesting a new
        # project. Warn but proceed — they may have a legitimate
        # reason (e.g. a sub-project for testing).
        if implicit_project_mode and _is_in_project():
            err_console.print(
                f"[yellow]⚠[/yellow] You're inside an existing movate "
                f"project, but [bold]mdk init {name}[/bold] is about "
                f"to create a NESTED project at [bold]{(target / name).resolve()}[/bold].\n"
                "[dim]If you meant to add an agent to the current "
                "project, run [bold]mdk add <template>[/bold] instead "
                f"(e.g. [bold]mdk add {name}[/bold]).[/dim]"
            )

        # When --with-agents is set, suppress the standalone Project
        # Panel and fold the project metadata into the combined Panel
        # that _scaffold_with_agents renders at the end. Otherwise
        # _init_project renders its existing Panel.
        project_name, project_root, snapshot_short = _init_project(
            name=name,
            target=target,
            force=force,
            skip_snapshot=skip_snapshot,
            with_agents=with_agents,
            quiet=bool(with_agents),
            open_editor=open_editor,
        )
        # --with-agents X,Y,Z: scaffold each role template inside the
        # freshly-bootstrapped project. Skipped when the operator is
        # bootstrapping in place (no name) AND with_agents isn't set,
        # but works in either layout when explicitly requested.
        if with_agents:
            _scaffold_with_agents(
                project_root=project_root,
                agents_csv=with_agents,
                force=force,
                project_name=project_name,
                snapshot_short=snapshot_short,
            )
        return

    if not name:
        # Either project mode in-place (operator typed `mdk init --project`
        # — handled above) or agent mode without a name (error).
        # Reaching here means agent intent (operator passed -t or --llm)
        # without a name. Surface the right hint.
        in_project = _is_in_project()
        if not in_project:
            err_console.print(
                "[red]✗[/red] name required.\n"
                "[dim]Most common uses:\n"
                "  [bold]mdk init my-project[/bold]            "
                "# bootstrap a new project (default)\n"
                "  [bold]mdk init --project[/bold]             "
                "# bootstrap the current directory in place\n"
                "  [bold]mdk init my-agent -t faq[/bold]       "
                "# add an agent (in-project only)[/dim]"
            )
        else:
            err_console.print(
                "[red]✗[/red] name required.\n"
                "[dim]You're already inside a movate project — to "
                "add an agent, use:\n"
                "  [bold]mdk add <template>[/bold]   "
                "(see [bold]mdk add --list[/bold])\n"
                "Or [bold]mdk init <name>[/bold] to nest a new "
                "project at ./<name>/.[/dim]"
            )
        raise typer.Exit(code=2)

    # Positional-description shorthand: `mdk init <name> "<description>"`
    # is equivalent to `mdk init <name> --llm "<description>"`. Operators
    # try this naturally — the wordy second positional reads as the
    # description without needing to know the --llm flag. When both
    # forms are passed, --llm wins (explicit beats implicit).
    if description and llm is None:
        llm = description
    elif description and llm is not None:
        err_console.print(
            "[yellow]⚠[/yellow] both a positional description and "
            "[bold]--llm[/bold] were passed — [bold]--llm[/bold] wins, "
            f"positional [dim]{description!r}[/dim] is ignored."
        )

    # Agent mode: dispatch to LLM-scaffold or template-scaffold path.
    # --llm + --template is allowed (the description guides which
    # template to start from); a warning surfaces so operators don't
    # silently get a mismatched starting point.
    #
    # Template default in agent mode is "default" (the echo template).
    # We use that fallback here rather than at parse time so the
    # implicit-project-mode dispatch above can distinguish "operator
    # passed -t" (agent intent) from "operator didn't pass -t"
    # (project intent).
    effective_template = template or "default"

    # ADR 026 D1: route the agent-scaffold paths through the context-aware
    # layout. OUTSIDE a project → bootstrap a runnable project that holds
    # the agent; INSIDE a project → add the agent to it; `--bare` → the
    # legacy standalone single-dir output. The scaffolders below write into
    # `layout.agent_parent` (so the agent lands at `layout.agent_dir`).
    #
    # --dry-run (LLM only) is a no-write preview: skip the project wrapper /
    # editor entirely so a preview doesn't materialize a project on disk.
    layout: _AgentLayout | None = None
    if not dry_run:
        layout = _resolve_agent_layout(
            name=name,
            target=target,
            bare=bare,
            force=force,
            skip_snapshot=skip_snapshot,
            open_editor=open_editor,
        )
        scaffold_target = layout.agent_parent
    else:
        scaffold_target = target

    if llm is not None:
        if effective_template != "default":
            err_console.print(
                f"[yellow]⚠[/yellow] [bold]--llm[/bold] + "
                f"[bold]--template {effective_template}[/bold] — the description "
                f"drives generation; the template is acknowledged as a starting "
                f"reference."
            )
        # ADR 026 D6: resolve the scaffold (driver) model by layered
        # precedence — flag > MDK_LLM_MODEL > project > user-config > default.
        resolved_llm_model = _resolve_scaffold_model(
            llm_model=llm_model,
            llm_model_explicit=(llm_model != _DEFAULT_LLM_MODEL),
        )
        _init_agent_from_llm(
            name=name,
            description=llm,
            llm_model=resolved_llm_model,
            target=scaffold_target,
            force=force,
            dry_run=dry_run,
            starting_template=effective_template,
            mock=mock,
            no_ingest=no_ingest,
            no_verify=no_verify,
        )
        if layout is not None:
            _finish_agent_init(
                layout,
                name=name,
                open_editor=open_editor,
                mock=mock,
                no_baseline=no_baseline,
            )
        return

    # No --llm: original template-copy path. --dry-run is meaningless
    # here today (template copy is cheap and idempotent); warn-don't-
    # error so we don't break muscle memory if operators sprinkle it.
    if dry_run:
        err_console.print(
            "[yellow]⚠[/yellow] [bold]--dry-run[/bold] is only meaningful "
            "with [bold]--llm[/bold]; ignored for template scaffold."
        )

    # Quiet the legacy plain-text scaffold output: the D4 next-steps panel
    # below renders the EXACT runnable command for the resolved layout.
    _init_agent(
        name=name,
        template=effective_template,
        target=scaffold_target,
        force=force,
        quiet=True,
    )
    if layout is not None:
        # ADR 026 D1 + F4': a template that declares skills/contexts must be
        # RUNNABLE after `mdk init -t`, not just after `mdk add`. `_init_agent`
        # only relocates skills BUNDLED in the template dir — declared-but-
        # unbundled skills (e.g. rag-qa's kb-vector-lookup) and declared
        # contexts are scaffolded the same way `mdk add` does, so the agent
        # loads (and the post-scaffold eval baseline below can run).
        _provision_template_skills_and_contexts(
            agent_dir=layout.agent_dir,
            template=effective_template,
            project_root=layout.project_root,
        )
        console.print(
            f"[green]✓[/green] scaffolded [bold]{effective_template}[/bold] agent "
            f"at [bold]{layout.agent_dir}[/bold]"
        )
        _finish_agent_init(
            layout,
            name=name,
            open_editor=open_editor,
            mock=mock,
            no_baseline=no_baseline,
        )
