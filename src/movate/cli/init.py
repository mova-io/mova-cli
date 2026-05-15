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
# {name} — movate project config (canonical: policy.yaml; movate.yaml
# is the legacy slot, still supported through v1.x).
#
# Loaded by every CLI command via `load_project_config`. Per-agent
# `agent.yaml` always wins on conflict; entries here only fill gaps.
# See `mdk doctor` for the layered semantics, and add `policy:` /
# `runtime:` / `skills:` blocks to gate every agent workspace-wide.

agents_dir: ./agents
workflows_dir: ./workflows

defaults:
  model:
    params:
      # Project-wide model param defaults. Agent.yaml's `model.params`
      # always wins per-key; these only fill keys the agent didn't
      # specify. Headline use: pin temperature / max_tokens once at
      # the project level instead of repeating across every agent.
      temperature: 0.0
      max_tokens: 512
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


def _is_in_project() -> bool:
    """Walk up from cwd looking for ``movate.yaml`` — the same
    convention :mod:`movate.cli.add_cmd` uses. Lets ``mdk init``
    surface a context-aware hint when called outside a project.
    """
    current = Path.cwd().resolve()
    while True:
        if (current / "movate.yaml").is_file():
            return True
        if current.parent == current:
            return False
        current = current.parent


# ---------------------------------------------------------------------------
# Project mode
# ---------------------------------------------------------------------------


def _init_project(
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
        # In-place bootstrap: refuse if there's already a movate.yaml,
        # unless --force is set. Avoids clobbering a real project.
        if (project_root / "movate.yaml").is_file() and not force:
            err_console.print(
                f"[red]✗[/red] {project_root}/movate.yaml already exists "
                "(use [bold]--force[/bold] to overwrite the project config)"
            )
            raise typer.Exit(code=2)
        project_root.mkdir(parents=True, exist_ok=True)

    # Project-level config files.
    (project_root / "movate.yaml").write_text(_PROJECT_MOVATE_YAML.format(name=project_name))
    (project_root / ".env.example").write_text(_PROJECT_ENV_EXAMPLE)
    (project_root / ".gitignore").write_text(_PROJECT_GITIGNORE)

    # Empty agents/ directory with a .gitkeep so it survives git add.
    agents_dir = project_root / "agents"
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / ".gitkeep").write_text("")

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
        f"[bold]Path:[/bold]      [cyan]{project_root}[/cyan]\n\n"
        f"  • [cyan]movate.yaml[/cyan]    project config\n"
        f"  • [cyan].env.example[/cyan]   env-var template\n"
        f"  • [cyan].gitignore[/cyan]     standard ignores\n"
        f"  • [cyan]agents/[/cyan]        empty (waiting for agents)\n"
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
    if with_agents:
        # Agents already added by the caller. Show forward-looking
        # commands: doctor agent / run / eval / deploy.
        agent_list = [t.strip() for t in with_agents.split(",") if t.strip()]
        first_agent = agent_list[0] if agent_list else "<agent>"
        body += (
            f"\n[bold]Next steps[/bold] "
            f"[dim](you already added {len(agent_list)} agent(s))[/dim][bold]:[/bold]\n"
            f"  [dim]$[/dim] [bold]cd {project_root.name}[/bold]\n"
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
        body += (
            f"\n[bold]Next steps:[/bold]\n"
            f"  [dim]$[/dim] [bold]cd {project_root.name} && mdk add --list[/bold]"
            f"   [dim]# browse role templates[/dim]\n"
            f"  [dim]$[/dim] [bold]mdk add rag-qa ticket-triager[/bold]"
            f"   [dim]# or batch-add any 2-3 roles[/dim]\n\n"
            f"[dim]Tip: skip the two-step flow next time with [bold]--with-agents[/bold]:[/dim]\n"
            f"  [dim]$ mdk init --project <name> --with-agents rag-qa,ticket-triager[/dim]\n\n"
            f"[dim]API keys: configured globally via "
            f"[bold]mdk auth login <provider>[/bold]. Per-project [bold].env[/bold] "
            f"still works as an override.[/dim]"
        )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] Project initialized",
            title_align="left",
            border_style="green",
        )
    )
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
        lines.append(
            f"[bold]Snapshot:[/bold]  [dim]{snapshot_short}[/dim] (initial baseline)"
        )
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
            "[green]✓[/green]" if validates == "true"
            else "[yellow]⚠[/yellow]" if validates == "false"
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
    lines.extend(
        [
            "",
            "[bold]Next steps:[/bold]",
            f"  [dim]$[/dim] [bold]cd {project_root.name}[/bold]",
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

    if quiet:
        return

    console.print(
        f"[green]✓[/green] scaffolded [bold]{template}[/bold] agent at [bold]{dest}[/bold]"
    )
    console.print("\nNext steps:")
    console.print(f"  movate validate {dest}")
    console.print(f"  movate run {dest} --mock '{{}}'   # provide input matching schema/input.json")
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
                    name=name, llm=True, model=llm_model,
                    tokens=total_tokens, ok=False, retried=True,
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
                    name=name, llm=True, model=llm_model,
                    tokens=total_tokens, ok=False, retried=True,
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
            name=name, llm=True, model=llm_model,
            tokens=total_tokens, ok=True, retried=retried,
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
        name=name, llm=True, model=llm_model,
        tokens=total_tokens, ok=True, retried=retried,
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


def _render_success_panel(
    *, name: str, dest: Path, generated: Any, cost_usd: float | None
) -> None:
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
            "[dim]→ preview only · re-run without [bold]--dry-run[/bold] "
            "to write files[/dim]"
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
            "[bold]mdk init faq-agent \"FAQ agent for our SaaS pricing\"[/bold]."
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
    template: str = typer.Option(
        "default",
        "--template",
        "-t",
        help=f"Template to scaffold from. One of: {', '.join(list_templates())}.",
    ),
    target: Path = typer.Option(
        Path("."), "--target", help="Parent directory for the new agent or project."
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
            "[bold]mdk init <agent-name> --llm \"<description>\"[/bold] "
            "inside it.[/dim]"
        )
        raise typer.Exit(code=2)

    if project:
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
        # Context-aware hint: if the cwd has no movate.yaml anywhere up
        # the tree, the operator is probably trying to bootstrap a
        # project. Steer them to the right flag.
        in_project = _is_in_project()
        if not in_project:
            err_console.print(
                "[red]✗[/red] agent name required. "
                "[dim]You're not in a movate project yet — try "
                "[bold]mdk init --project <name>[/bold] to bootstrap "
                "one first.[/dim]"
            )
        else:
            err_console.print(
                "[red]✗[/red] agent name required. "
                "[dim]Run [bold]mdk init --help[/bold] for usage, or pass "
                "[bold]--project[/bold] to bootstrap a project instead.[/dim]"
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
    if llm is not None:
        if template != "default":
            err_console.print(
                f"[yellow]⚠[/yellow] [bold]--llm[/bold] + "
                f"[bold]--template {template}[/bold] — the template will "
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
            starting_template=template,
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

    _init_agent(name=name, template=template, target=target, force=force)
