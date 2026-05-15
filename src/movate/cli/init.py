"""``movate init`` — scaffold a new agent OR bootstrap a fresh project.

Two modes:

* **Agent mode** (default): ``movate init <name>`` scaffolds one agent
  directory under ``<target>/<name>/`` from a packaged template. Same
  behavior shipped pre-Sprint P.

* **Project mode** (``--project``): bootstrap a fresh movate workspace
  with ``movate.yaml`` + ``.env.example`` + ``.gitignore`` + empty
  ``agents/``. Auto-creates an initial snapshot so the operator has
  a baseline for ``mdk diff`` / ``mdk rollback`` immediately.

Project mode is the "step 0" before any agents exist. Agent mode is
the "step 1+" inside an existing project. ``mdk demo`` is the third
sibling: full populated project (project + working agent + dataset).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from movate.templates import get_template_path, list_templates

console = Console()
err_console = Console(stderr=True)


# Project-mode files. Kept inline (not separate templates) for the same
# reason `mdk demo` does — they're tiny and inlining keeps the recipe
# legible in one read. If they grow, lift to src/movate/templates/.
_PROJECT_MOVATE_YAML = """\
api_version: movate/v1
kind: Project
name: {name}
description: ""
version: 0.1.0

defaults:
  model:
    provider: openai/gpt-4o-mini-2024-07-18
    params:
      temperature: 0.0
      max_tokens: 512

storage:
  backend: sqlite
  path: .movate/local.db
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


# ---------------------------------------------------------------------------
# Project mode
# ---------------------------------------------------------------------------


def _init_project(
    *,
    name: str | None,
    target: Path,
    force: bool,
    skip_snapshot: bool,
) -> None:
    """Bootstrap a fresh movate workspace.

    Two layouts depending on ``name``:

    * ``name`` given:   creates ``<target>/<name>/`` as the project root.
    * ``name`` blank:   bootstraps ``<target>`` itself in place.

    Either way, the resulting directory gets ``movate.yaml`` +
    ``.env.example`` + ``.gitignore`` + an empty ``agents/`` dir with
    a ``.gitkeep`` placeholder. Then we auto-snapshot — operators get
    a baseline to ``mdk diff`` / ``mdk rollback`` against from day one.
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
    body += (
        f"\n[bold]Next steps:[/bold]\n"
        f"  [dim]$[/dim] [bold]cd {project_root.name}[/bold]\n"
        f"  [dim]$[/dim] [bold]cp .env.example .env[/bold]"
        f"   [dim]# then add your API key[/dim]\n"
        f"  [dim]$[/dim] [bold]mdk init <agent-name>[/bold]  "
        f"[dim]# scaffold an agent[/dim]"
    )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] Project initialized",
            title_align="left",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# Agent mode (the original behavior, preserved verbatim)
# ---------------------------------------------------------------------------


def _init_agent(
    *,
    name: str,
    template: str,
    target: Path,
    force: bool,
) -> None:
    """Scaffold a single agent directory from a packaged template."""
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
    """
    if project:
        _init_project(
            name=name,
            target=target,
            force=force,
            skip_snapshot=skip_snapshot,
        )
        return

    if not name:
        err_console.print(
            "[red]✗[/red] agent name required. "
            "[dim]Run [bold]mdk init --help[/bold] for usage, or pass "
            "[bold]--project[/bold] to bootstrap a project instead.[/dim]"
        )
        raise typer.Exit(code=2)

    _init_agent(name=name, template=template, target=target, force=force)
