"""``movate init`` — scaffold a new agent OR bootstrap a fresh project.

Three modes:

* **Agent mode** (default): ``movate init <name>`` scaffolds one agent
  directory under ``<target>/<name>/`` from a packaged template. Same
  behavior shipped pre-Sprint P.

* **Project mode** (``--project``): bootstrap a fresh movate workspace
  with ``movate.yaml`` + ``.env.example`` + ``.gitignore`` + empty
  ``agents/``. Auto-creates an initial snapshot so the operator has
  a baseline for ``mdk diff`` / ``mdk rollback`` immediately.

* **LLM-scaffold mode** (``--llm "<description>"``): generate the
  agent from a natural-language description using an LLM. The
  generator (in :mod:`movate.scaffold`) calls the configured provider
  with a meta-prompt + two few-shot exemplars, parses the response
  into a :class:`GeneratedAgent`, writes it to a tempdir, and
  validates by loading it back through :func:`load_agent`. On
  validation failure the error is fed back to the LLM for one retry;
  a second failure stashes the raw payload at
  ``.movate/llm-init-failed-<name>.json`` and exits 1. Successful
  scaffolds emit a Rich Panel with the file list + cost + next-step
  commands, an ``_console.hint`` line pointing at ``prompt.md``, and a
  greppable ``mdk_init_summary:`` line for CI parity with
  ``mdk_audit_summary`` / ``mdk_eval_summary`` / ``mdk_doctor_summary``.

  Pair with ``--mock`` for hermetic CI (no API keys); ``--dry-run``
  renders a preview Panel without writing files; ``--llm-model``
  overrides the default (``openai/gpt-4o-mini-2024-07-18``).

Project mode is the "step 0" before any agents exist. Agent and
LLM-scaffold modes are the "step 1+" inside an existing project.
``mdk demo`` is the fourth sibling: a fully populated reference
project (project + working agent + dataset).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

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
# About .movate/ — runtime state directory
# =============================================================================
#
# Created in the project root when you run any `mdk` command that
# needs persistent state. Layout:
#
#   .movate/
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
# shipped when". Drop `.movate/snapshots/` from `.gitignore` if you'd
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
.movate/local.db
.movate/local.db-*

# Snapshots are commit-friendly by default (content-addressed,
# small) but operators can opt out of tracking them in git:
# .movate/snapshots/

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
    # Pick an editor command best-effort. Most operators on macOS/Linux
    # have `code` (VS Code); we fall back to `open` (macOS Finder) so
    # the action always runs even without VS Code installed. Windows
    # operators can pick option [2]/[3] instead.
    import shutil as _shutil  # noqa: PLC0415

    editor_cmd: str | None = None
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

    next_steps = []
    if editor_cmd is not None:
        next_steps.append(NextStep(label=editor_label, command=editor_cmd, argv=editor_argv))
    next_steps.extend(
        [
            NextStep(
                label="Browse role templates",
                command=f"{bin_name} templates list",
                argv=[bin_name, "templates", "list"],
            ),
            NextStep(
                label="Add the FAQ agent",
                command=f"cd {cd_to} && {bin_name} add faq",
                argv=["sh", "-c", f"cd {cd_to} && {bin_name} add faq"],
            ),
            NextStep(
                label="Add two role agents (rag-qa + ticket-triager)",
                command=f"cd {cd_to} && {bin_name} add rag-qa ticket-triager",
                argv=[
                    "sh",
                    "-c",
                    f"cd {cd_to} && {bin_name} add rag-qa ticket-triager",
                ],
            ),
        ]
    )
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


_PROJECT_MARKERS = ("movate.yaml", "policy.yaml")


def _find_project_root(from_dir: Path) -> Path | None:
    """Walk up from ``from_dir`` looking for a project root marker.

    Returns the first ancestor that contains ``movate.yaml`` or
    ``policy.yaml``, or ``None`` if no marker is found. Used by
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
        d for d in bundled_skills_dir.iterdir()
        if d.is_dir() and d.name != "example-skill"
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
    console.print(f"  mdk run {dest} --mock '{{}}'   # provide input matching schema/input.json")
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

# Where Phase 2 stashes a failed-second-attempt's raw payload for the
# operator to inspect. Relative to the cwd at invocation time — the
# project root in the normal flow. Operators are pointed at this path
# in the error message so they don't have to grep stderr.
_DEBUG_ARTIFACT_REL = ".movate/llm-init-failed-{name}.json"

# Preview truncation cap for the prompt body in --dry-run mode. Long
# enough that the operator sees the agent's intent; short enough that
# Rich Panel rendering stays compact.
_DRY_RUN_PROMPT_PREVIEW_CHARS = 600


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
) -> None:
    """Scaffold an agent from a natural-language description.

    The flow is:

    1. Build a local runtime (:func:`build_local_runtime`) so we get a
       provider configured the same way as :command:`mdk run` does.
    2. Call :func:`generate_agent_from_description` once.
    3. Write to a tempdir; run :func:`load_agent` to validate end-to-end.
    4. On validation failure: re-prompt with the error context and retry
       ONCE. On second failure: stash raw JSON to
       ``.movate/llm-init-failed-<name>.json`` and exit 2.
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
    from movate.scaffold import (  # noqa: PLC0415
        LLMScaffoldError,
        generate_agent_from_description,
        write_agent_files,
    )

    # Roll token usage across every LLM call (attempt 1 + retry) so the
    # final cost line reflects total spend. Used by the cost echo +
    # mdk_init_summary line at the end.
    total_tokens = TokenUsage()
    # Track whether a retry actually fired — used by the summary line
    # so CI dashboards can flag "this scaffold needed correction" runs.
    retried = False

    rt = await build_local_runtime(mock=mock)
    try:
        # Attempt 1 — fresh generation from the description.
        try:
            with spinner(f"scaffolding agent '{name}' from description..."):
                result = await generate_agent_from_description(
                    description=description,
                    name=name,
                    model=llm_model,
                    provider=rt.provider,
                )
            total_tokens = _accumulate_tokens(total_tokens, result.tokens)
            generated = result.agent
        except LLMScaffoldError as exc:
            err_console.print(f"[red]✗[/red] LLM scaffold failed: {exc}")
            raise typer.Exit(code=2) from None

        # Enforce the name-constraint defensively. A forgetful LLM might
        # echo the example's name ("faq-agent") instead of honoring the
        # description's requested name. We override AFTER generation so
        # the dir/file/agent-yaml correspondence is always preserved.
        # If the LLM hallucinated a *different* name, that's a soft
        # failure: we silently coerce. (Add a warning here if pilot data
        # shows real LLMs ignoring this constraint at meaningful rates.)
        generated.agent_yaml["name"] = name

        # Validate by writing to a tempdir and loading.
        validation_error = _try_validate(generated, name=name)

        # Retry once if validation failed.
        if validation_error is not None:
            retried = True
            err_console.print(
                f"[yellow]⚠[/yellow] first attempt failed validation: "
                f"[dim]{validation_error}[/dim]\n"
                f"[dim]retrying once with the error fed back to the model...[/dim]"
            )
            try:
                with spinner(f"retrying scaffold for '{name}'..."):
                    result = await generate_agent_from_description(
                        description=description,
                        name=name,
                        model=llm_model,
                        provider=rt.provider,
                        previous_attempt=generated,
                        validation_error=validation_error,
                    )
                total_tokens = _accumulate_tokens(total_tokens, result.tokens)
                generated = result.agent
                generated.agent_yaml["name"] = name
            except LLMScaffoldError as exc:
                _save_debug_artifact(name, payload=None, raw_error=str(exc))
                err_console.print(
                    f"[red]✗[/red] retry also failed: {exc}\n"
                    f"[dim]raw error saved to "
                    f"[bold]{_DEBUG_ARTIFACT_REL.format(name=name)}[/bold][/dim]"
                )
                _print_init_summary_line(
                    name=name,
                    llm=True,
                    model=llm_model,
                    tokens=total_tokens,
                    ok=False,
                    retried=True,
                )
                raise typer.Exit(code=2) from None

            validation_error = _try_validate(generated, name=name)
            if validation_error is not None:
                _save_debug_artifact(name, payload=generated, raw_error=validation_error)
                err_console.print(
                    f"[red]✗[/red] retry attempt also failed validation:\n"
                    f"[dim]{validation_error}[/dim]\n"
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
                    retried=True,
                )
                raise typer.Exit(code=1)
    finally:
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

    _render_success_panel(name=name, dest=dest, generated=generated, cost_usd=cost_usd)
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
    """Write ``generated`` to a tempdir and run :func:`load_agent`.

    Returns ``None`` on success, or the error string on failure. The
    string is fed back to the retry prompt so the LLM can self-correct.
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
        try:
            load_agent(tmp_agent_dir)
        except AgentLoadError as exc:
            return str(exc)
    return None


def _save_debug_artifact(name: str, *, payload: Any, raw_error: str) -> None:
    """Stash the failed LLM output to ``.movate/llm-init-failed-<name>.json``."""
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
    import json as _json  # noqa: PLC0415

    import yaml as _yaml  # noqa: PLC0415

    body = (
        f"[bold]Agent:[/bold]   [cyan]{name}[/cyan]\n"
        f"[bold]Target:[/bold]  [dim]{dest}[/dim] [yellow](dry-run; not written)[/yellow]\n\n"
        f"[bold]agent.yaml:[/bold]\n"
        f"[dim]{_yaml.safe_dump(generated.agent_yaml, sort_keys=False).strip()}[/dim]\n\n"
        f"[bold]prompt.md:[/bold]\n"
        f"[dim]{generated.prompt_md.strip()[:_DRY_RUN_PROMPT_PREVIEW_CHARS]}"
        f"{'…' if len(generated.prompt_md) > _DRY_RUN_PROMPT_PREVIEW_CHARS else ''}[/dim]\n\n"
        f"[bold]schema/input.json:[/bold]\n"
        f"[dim]{_json.dumps(generated.input_schema, indent=2)}[/dim]\n\n"
        f"[bold]schema/output.json:[/bold]\n"
        f"[dim]{_json.dumps(generated.output_schema, indent=2)}[/dim]\n\n"
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


def _render_success_panel(*, name: str, dest: Path, generated: Any, cost_usd: float | None) -> None:
    """Print the success Panel — mirrors the template-copy success path."""
    body = (
        f"[bold]Agent:[/bold]    [cyan]{name}[/cyan]\n"
        f"[bold]Path:[/bold]     [cyan]{dest}[/cyan]\n"
        f"[bold]Files:[/bold]\n"
        f"  • [cyan]agent.yaml[/cyan]\n"
        f"  • [cyan]prompt.md[/cyan]\n"
        f"  • [cyan]schema/input.json[/cyan]\n"
        f"  • [cyan]schema/output.json[/cyan]\n"
    )
    if generated.sample_evals:
        body += (
            f"  • [cyan]evals/dataset.jsonl[/cyan] "
            f"[dim]({len(generated.sample_evals)} seed cases)[/dim]\n"
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
        f"[dim]scaffolded by --llm · review prompt.md and the schemas "
        f"before first real run.[/dim]"
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
# Entry point — dispatches between project + agent modes
# ---------------------------------------------------------------------------


def init(
    name: str = typer.Argument(
        None,
        help=(
            "Agent name (default mode) OR project name (with [bold]--project[/bold]). "
            "Lowercase, hyphenated. Omit with [bold]--project[/bold] to bootstrap "
            "the current directory in place."
        ),
    ),
    description: str = typer.Argument(
        None,
        help=(
            "Optional natural-language description. When set, treated as "
            "shorthand for [bold]--llm[/bold]: "
            '[bold]mdk init faq-agent "FAQ agent for our SaaS pricing"[/bold].'
        ),
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help=(
            "Bootstrap a fresh movate project workspace instead of scaffolding "
            "an agent. Creates [bold]movate.yaml[/bold] + [bold].env.example[/bold] + "
            "[bold].gitignore[/bold] + empty [bold]agents/[/bold] + an initial snapshot."
        ),
    ),
    template: str | None = typer.Option(
        None,
        "--template",
        "-t",
        help=(
            f"Template to scaffold from. One of: {', '.join(list_templates())}. "
            "When set (explicitly), runs in AGENT mode and produces "
            "an agent scaffold rather than a project. Without "
            "[bold]-t[/bold] (and without [bold]--llm[/bold]), "
            "[bold]mdk init <name>[/bold] defaults to PROJECT mode."
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
            "Preview the generated files without writing to disk. Only "
            "meaningful with [bold]--llm[/bold] today; ignored otherwise."
        ),
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help=(
            "Use the deterministic [bold]MockProvider[/bold] for the LLM call. "
            "Hermetic CI mode — no API keys required. Only meaningful with "
            "[bold]--llm[/bold]; ignored otherwise."
        ),
    ),
) -> None:
    """Scaffold a new agent, or bootstrap a fresh project workspace.

    [bold]Project mode:[/bold] [bold]mdk init --project [my-proj][/bold]
    creates a fresh movate workspace with project config + .gitignore +
    empty agents/ + an initial snapshot. Omit the name to bootstrap the
    current directory in place.

    [bold]Agent mode:[/bold] [bold]mdk init <name>[/bold] scaffolds one
    agent inside an existing project. Pick a template with
    [bold]--template[/bold].

    [bold]Available agent templates:[/bold]

      [bold]default[/bold]    — minimal echo agent (string-in, string-out)
      [bold]faq[/bold]        — question → answer + confidence
      [bold]summarizer[/bold] — text + max_words → summary + word_count
      [bold]classifier[/bold] — text + labels → chosen label
      [bold]chatbot[/bold]    — message → reply (designed for `mdk chat`)
      [bold]extractor[/bold]  — text → strict typed fields

    [bold]Examples:[/bold]

      [dim]$ mdk init --project my-proj[/dim]
      [dim]$ mdk init --project        # bootstrap current directory[/dim]
      [dim]$ mdk init faq               # add one agent from the faq template[/dim]
      [dim]$ mdk init my-bot --template chatbot[/dim]
      [dim]$ mdk init faq-agent --llm "FAQ agent for our SaaS pricing"  # Phase 2[/dim]
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
    # silently get a mismatched starting point. Phase 2's generator
    # will honor the template as a few-shot exemplar.
    #
    # Template default in agent mode is "default" (the echo template).
    # We use that fallback here rather than at parse time so the
    # implicit-project-mode dispatch above can distinguish "operator
    # passed -t" (agent intent) from "operator didn't pass -t"
    # (project intent).
    effective_template = template or "default"
    if llm is not None:
        if effective_template != "default":
            err_console.print(
                f"[yellow]⚠[/yellow] [bold]--llm[/bold] + "
                f"[bold]--template {effective_template}[/bold] — the template will "
                f"seed the few-shot prompt as a starting structure. "
                f"[dim](Phase 2 will honor this; Phase 1 just acknowledges "
                f"the combination.)[/dim]"
            )
        _init_agent_from_llm(
            name=name,
            description=llm,
            llm_model=llm_model,
            target=target,
            force=force,
            dry_run=dry_run,
            starting_template=effective_template,
            mock=mock,
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

    _init_agent(name=name, template=effective_template, target=target, force=force)
