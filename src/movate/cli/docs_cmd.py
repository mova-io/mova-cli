"""``mdk docs`` — auto-generated project documentation (Sprint P).

Subcommands:

* ``runbook`` — generate an ops-friendly markdown runbook from
  the project's current state. Includes agents, env vars, common
  recipes, troubleshooting, and a state-cluster cheatsheet.

Future siblings (post-Sprint P):

* ``api`` — generate OpenAPI / Markdown docs for the deployed
  ``mdk serve`` endpoints once Phase 5 lands.
* ``changelog`` — synthesize CHANGELOG from snapshot descriptions
  + promotion log entries.

Design rules followed by all ``mdk docs *`` subcommands:

1. **No external service calls** at generation time. Pure
   filesystem inspection so docs work offline + in CI.
2. **Default to writing under** ``docs/`` so generated artifacts
   are easy to track in git + ignore separately if desired.
3. **--dry-run prints to stdout** so operators can preview / pipe
   to other tools without committing anything to disk.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from movate.docs import build_context, generate_runbook

console = Console()
err_console = Console(stderr=True)


docs_app = typer.Typer(
    name="docs",
    help=(
        "Auto-generated project documentation. "
        "[bold]mdk docs runbook[/bold] generates a markdown ops runbook from "
        "the current project state — refresh after significant changes."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Subcommand: runbook
# ---------------------------------------------------------------------------


@docs_app.command("runbook")
def runbook(
    output: Path = typer.Argument(
        Path("docs/RUNBOOK.md"),
        help="Where to write the generated markdown (default: docs/RUNBOOK.md).",
        metavar="OUTPUT",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite OUTPUT if it already exists.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the generated markdown to stdout without writing.",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd).",
        hidden=True,
    ),
) -> None:
    """Generate an ops runbook from the current project state.

    The runbook captures: project metadata, every agent (with model +
    eval-dataset status), discovered env vars (required vs optional),
    common operation recipes, the state-cluster cheatsheet, and a
    troubleshooting section.

    Re-run after significant changes — the doc stays useful only as
    long as it matches reality.

    [bold]Examples:[/bold]

      [dim]$ mdk docs runbook                          # writes docs/RUNBOOK.md[/dim]
      [dim]$ mdk docs runbook --dry-run                # preview, no write[/dim]
      [dim]$ mdk docs runbook ops/runbook.md --force   # custom location[/dim]
    """
    root = Path(project_root).resolve()

    # Build context + render. Both are pure functions; nothing on disk
    # changes until the write step below.
    context = build_context(root)
    markdown = generate_runbook(context)

    if dry_run:
        # Plain stdout — operators may pipe this to less / pandoc /
        # whatever. No Rich panel around the body.
        typer.echo(markdown, nl=False)
        return

    target = output if output.is_absolute() else (root / output)
    target = target.resolve()

    if target.exists() and not force:
        err_console.print(
            f"[red]✗[/red] {target} already exists (use [bold]--force[/bold] to overwrite)"
        )
        raise typer.Exit(code=2)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown)

    body = (
        f"[bold]Wrote:[/bold]   [cyan]{target}[/cyan]\n"
        f"[bold]Agents:[/bold]  {len(context.agents)}\n"
        f"[bold]Env vars:[/bold] {len(context.required_env_vars)} required, "
        f"{len(context.optional_env_vars)} optional"
    )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] Runbook generated",
            title_align="left",
            border_style="green",
        )
    )
