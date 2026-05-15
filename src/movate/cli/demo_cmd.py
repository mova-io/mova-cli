"""``mdk demo`` — one-command runnable demo project (Sprint P).

Generates a complete, working FAQ agent + dataset + project structure
in 60 seconds. Different from ``mdk init`` (which scaffolds an empty
template into an existing project): ``mdk demo`` is the "from zero"
hello-world for sales demos, onboarding, and operator first-touch.

Default output::

  demo-faq/
    movate.yaml                 # project config
    .env.example                # required env var template
    .gitignore                  # standard movate ignores
    agents/faq/
      agent.yaml                # working FAQ agent
      prompt.md
      schema/{input,output}.json
      evals/dataset.jsonl       # 3 sample Q/A cases
      evals/judge.yaml.example  # judge-eval template

The recipe is fixed for MVP — one canonical FAQ demo. Future
enhancements (``--template chatbot``, ``--template classifier``)
parameterize which template directory gets copied in; the
:func:`_demo_recipe` indirection keeps that swap-in cheap.
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

console = Console()
err_console = Console(stderr=True)


# The agent template package + the demo's destination layout.
# Lifting the template package out as a constant so a future
# `--template <name>` flag is a one-line dispatch.
_TEMPLATE_PACKAGE = "movate.templates.faq_agent"
_AGENT_NAME = "faq"


# Project-level files that get written alongside the agent copy.
# Kept as inline strings (not separate template files) because
# they're tiny and inlining keeps the demo recipe legible in one read.
_MOVATE_YAML = """\
api_version: movate/v1
kind: Project
name: demo-faq
description: One-command runnable demo — an FAQ agent with sample eval dataset.
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

_ENV_EXAMPLE = """\
# Demo requires one provider key. Uncomment + set ONE of the following:

OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
# AZURE_API_KEY=

# Optional — enables Langfuse tracing if set:
# LANGFUSE_PUBLIC_KEY=
# LANGFUSE_SECRET_KEY=
"""

_GITIGNORE = """\
# movate runtime state — never commit
.movate/

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
# Helpers
# ---------------------------------------------------------------------------


def _resolve_template_root() -> Path:
    """Return the on-disk path to the FAQ template directory.

    Resolves via the parent package's ``__file__`` so the lookup works
    for both editable installs (source tree) and wheel installs (real
    filesystem paths inside site-packages). We need a real path —
    ``shutil.copytree`` doesn't accept the ``MultiplexedPath`` /
    ``Traversable`` objects ``importlib.resources.files`` may return.

    Falls through ``importlib`` as a secondary resolution path so the
    function still works if movate is ever bundled into a zip wheel.
    """
    # Primary: derive from the package's __file__. Works for source +
    # standard wheel installs.
    import movate.templates  # noqa: PLC0415 — deferred so import cycles can't deadlock

    pkg_root = Path(movate.templates.__file__).parent
    candidate = pkg_root / "faq_agent"
    if candidate.is_dir():
        return candidate

    # Fallback: importlib.resources, then materialize to a Path.
    # For zip-installed wheels this requires `as_file()` to extract;
    # we don't currently ship that way, but the path is here for safety.
    try:
        root = resources.files(_TEMPLATE_PACKAGE)
        path = Path(str(root))
        if path.is_dir():
            return path
    except (ModuleNotFoundError, FileNotFoundError):
        pass

    err_console.print(
        f"[red]✗[/red] demo template not found at {candidate}. "
        "[dim]Reinstall mdk or check movate.templates is packaged.[/dim]"
    )
    raise typer.Exit(code=2)


def _materialize_agent(template_root: Path, agent_dir: Path) -> None:
    """Copy the template into ``agent_dir`` and substitute __AGENT_NAME__.

    The template's ``agent.yaml`` uses ``__AGENT_NAME__`` as a sentinel —
    same convention as ``mdk init``. Substitution is plain string-replace
    (no Jinja for the demo path; the sentinel is unique enough).
    """
    shutil.copytree(template_root, agent_dir, dirs_exist_ok=False)

    agent_yaml = agent_dir / "agent.yaml"
    if agent_yaml.is_file():
        text = agent_yaml.read_text()
        agent_yaml.write_text(text.replace("__AGENT_NAME__", _AGENT_NAME))


def _write_project_files(target: Path) -> list[Path]:
    """Write movate.yaml, .env.example, .gitignore. Returns paths created.

    Returned list drives the "created N files" summary at the end —
    keep it order-stable so operators see consistent output across runs.
    """
    created: list[Path] = []

    movate_yaml = target / "movate.yaml"
    movate_yaml.write_text(_MOVATE_YAML)
    created.append(movate_yaml)

    env_example = target / ".env.example"
    env_example.write_text(_ENV_EXAMPLE)
    created.append(env_example)

    gitignore = target / ".gitignore"
    gitignore.write_text(_GITIGNORE)
    created.append(gitignore)

    return created


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def demo(
    directory: str = typer.Argument(
        "demo-faq",
        help="Directory to create the demo in (default: ./demo-faq).",
        metavar="DIR",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "Overwrite DIR if it already exists. "
            "[bold red]Destructive[/bold red] — wipes the existing contents."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be created without writing.",
    ),
) -> None:
    """Generate a complete runnable demo project (FAQ agent + dataset).

    The output is a self-contained directory you can ``cd`` into and
    run immediately:

      [dim]$ mdk demo[/dim]
      [dim]$ cd demo-faq[/dim]
      [dim]$ cp .env.example .env  # then add your API key[/dim]
      [dim]$ mdk run faq '{"question": "What is Python?"}'[/dim]
      [dim]$ mdk eval faq[/dim]

    [bold]Different from [bold]mdk init[/bold]:[/bold] init scaffolds
    one empty agent into an existing project. [bold]demo[/bold] creates
    a full project with a working agent, prompt, schemas, and a sample
    eval dataset — the 60-second hello-world.

    [bold]Examples:[/bold]

      [dim]$ mdk demo                       # creates ./demo-faq/[/dim]
      [dim]$ mdk demo my-first-agent        # custom directory name[/dim]
      [dim]$ mdk demo --force               # overwrite existing[/dim]
      [dim]$ mdk demo --dry-run             # preview only[/dim]
    """
    target = Path(directory).resolve()

    if target.exists() and not force:
        err_console.print(
            f"[red]✗[/red] {target} already exists. "
            "[dim]Pass [bold]--force[/bold] to overwrite, or pick a different "
            "directory name.[/dim]"
        )
        raise typer.Exit(code=2)

    template_root = _resolve_template_root()
    agent_dir = target / "agents" / _AGENT_NAME

    if dry_run:
        body = (
            f"[bold]Would create:[/bold]\n"
            f"  [cyan]{target}/[/cyan]\n"
            f"  [cyan]{target}/movate.yaml[/cyan]\n"
            f"  [cyan]{target}/.env.example[/cyan]\n"
            f"  [cyan]{target}/.gitignore[/cyan]\n"
            f"  [cyan]{agent_dir}/[/cyan]  [dim](from {_TEMPLATE_PACKAGE})[/dim]"
        )
        console.print(
            Panel(
                body + "\n\n[yellow]⚠ dry-run — nothing written.[/yellow]",
                title="mdk demo — preview",
                title_align="left",
                border_style="yellow",
            )
        )
        return

    # Wipe-and-recreate when --force; otherwise create fresh.
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    project_files = _write_project_files(target)
    _materialize_agent(template_root, agent_dir)

    # Success summary + next-step recipe.
    body = (
        f"[bold]Created:[/bold] [cyan]{target}[/cyan]\n\n"
        f"  • [cyan]movate.yaml[/cyan]            project config\n"
        f"  • [cyan].env.example[/cyan]           env-var template (copy to .env)\n"
        f"  • [cyan].gitignore[/cyan]             standard movate ignores\n"
        f"  • [cyan]agents/{_AGENT_NAME}/[/cyan]              "
        "working FAQ agent + dataset\n\n"
        f"[bold]Next steps:[/bold]\n"
        f"  [dim]$[/dim] [bold]cd {target.name}[/bold]\n"
        f"  [dim]$[/dim] [bold]cp .env.example .env[/bold]   [dim]# then add an API key[/dim]\n"
        f"  [dim]$[/dim] [bold]mdk run {_AGENT_NAME} "
        f'\'{{"question": "What is Python?"}}\'[/bold]\n'
        f"  [dim]$[/dim] [bold]mdk eval {_AGENT_NAME}[/bold]"
    )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] Demo ready",
            title_align="left",
            border_style="green",
        )
    )
    # Avoid lint warning about unused list — operators can inspect via
    # follow-up commands; the panel above is the canonical summary.
    _ = project_files
