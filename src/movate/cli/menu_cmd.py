"""``mdk menu`` — guided next-step UX (Sprint P onboarding polish).

Shows a compact workspace-status panel + 5-7 contextual action
suggestions. Picking one prints the literal command and (after a
y/N confirm) executes it.

Design calls:

* **Educational over magical.** Every menu entry shows the exact
  ``mdk <subcmd>`` it maps to, so the menu is a learning tool, not
  a black-box wizard.
* **Composable.** Picking an action shells out via :mod:`subprocess`
  to the same binary the operator invoked us with (preserves the
  ``mdk`` vs ``movate`` alias). No alternate code paths.
* **Always safe to ``Ctrl+C``.** Nothing is written before the
  confirm-then-exec step.
* **Cheap to render.** Status inspection is pure filesystem checks
  (<100ms typical); no LLM/Azure calls. For the heavy diagnostics
  use ``mdk doctor`` — which the menu helpfully surfaces as one
  of its always-on suggestions.
"""

from __future__ import annotations

import os
import subprocess
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from movate.menu import WorkspaceStatus, build_actions, inspect_workspace

console = Console()
err_console = Console(stderr=True)

# Cap rendered name/var lists so the status panel stays scannable.
# Above this, we collapse the rest into "+N more".
_NAME_LIST_LIMIT = 3


# ---------------------------------------------------------------------------
# Status rendering
# ---------------------------------------------------------------------------


def _render_status(status: WorkspaceStatus) -> None:
    """Render the compact workspace-status panel.

    Mirrors the visual style of ``mdk doctor`` but stays surgical:
    one line per dimension, marks with ✓ / ⚠ / ✗ for at-a-glance
    triage. Designed to fit in ~6-10 terminal rows.
    """
    rows: list[tuple[str, str]] = []

    rows.append(_yaml_row(status))
    rows.append(_profile_row(status))
    rows.append(_agents_row(status))
    rows.append(_env_row(status))
    rows.append(_snapshots_row(status))
    rows.append(_db_row(status))

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    for left, right in rows:
        table.add_row(left, right)

    console.print(
        Panel(
            table,
            title=f"[bold]Workspace[/bold] [dim]{status.project_root}[/dim]",
            title_align="left",
            border_style="cyan",
        )
    )


def _yaml_row(status: WorkspaceStatus) -> tuple[str, str]:
    # Post-PR #85 "project.yaml" is the canonical config-file name;
    # the WorkspaceStatus field name is historical (predates the
    # rename). Render the canonical label so the menu doesn't say
    # "movate.yaml" when the actual file is project.yaml.
    if status.has_movate_yaml:
        version = status.movate_yaml_version or "?"
        return ("[green]✓[/green]", f"project.yaml  [dim]({version})[/dim]")
    return (
        "[red]✗[/red]",
        "project.yaml  [dim]not initialized — run [bold]mdk init <name>[/bold][/dim]",
    )


def _profile_row(status: WorkspaceStatus) -> tuple[str, str]:
    if status.active_profile:
        return (
            "[green]✓[/green]",
            f"Active profile  [cyan]{status.active_profile}[/cyan]",
        )
    return (
        "[yellow]⚠[/yellow]",
        "Active profile  [dim]none — [bold]mdk profiles use <name>[/bold][/dim]",
    )


def _agents_row(status: WorkspaceStatus) -> tuple[str, str]:
    if not status.has_agents:
        return (
            "[red]✗[/red]",
            "Agents  [dim]none — [bold]mdk init <name>[/bold] to scaffold[/dim]",
        )
    names = ", ".join(a.name for a in status.agents[:_NAME_LIST_LIMIT])
    extra = len(status.agents) - _NAME_LIST_LIMIT
    overflow = "" if extra <= 0 else f", +{extra} more"
    return (
        "[green]✓[/green]",
        f"Agents ({len(status.agents)})  [dim]{names}{overflow}[/dim]",
    )


def _env_row(status: WorkspaceStatus) -> tuple[str, str]:
    missing = status.missing_env_vars
    if missing:
        names = ", ".join(v.name for v in missing[:_NAME_LIST_LIMIT])
        extra = len(missing) - _NAME_LIST_LIMIT
        overflow = "" if extra <= 0 else f", +{extra}"
        return ("[yellow]⚠[/yellow]", f".env  [dim]missing: {names}{overflow}[/dim]")
    if status.has_dotenv_file or status.env_vars:
        return ("[green]✓[/green]", ".env  [dim]all expected vars set[/dim]")
    return ("[yellow]⚠[/yellow]", ".env  [dim]no .env or .env.example detected[/dim]")


def _snapshots_row(status: WorkspaceStatus) -> tuple[str, str]:
    if status.snapshot_count == 0:
        return ("[dim]·[/dim]", "Snapshots  [dim]none taken yet[/dim]")
    return (
        "[green]✓[/green]",
        f"Snapshots  [dim]{status.snapshot_count} stored locally[/dim]",
    )


def _db_row(status: WorkspaceStatus) -> tuple[str, str]:
    if status.has_local_db:
        return ("[green]✓[/green]", "Local DB  [dim].movate/local.db present[/dim]")
    return ("[dim]·[/dim]", "Local DB  [dim]no runs recorded yet[/dim]")


# ---------------------------------------------------------------------------
# Action rendering + selection
# ---------------------------------------------------------------------------


def _render_actions(actions: list, console_: Console = console) -> None:
    """Render the numbered action menu.

    Two columns: the human label on the left, the literal command
    on the right (dimmed). The dimmed-command column is the
    *educational* part — operators learn the surface area without
    having to mash Tab through ``mdk --help``.
    """
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    table.add_column(style="dim", no_wrap=True)

    for idx, action in enumerate(actions, start=1):
        marker = f"[{idx}]"
        suffix = "  [yellow](needs input)[/yellow]" if action.needs_user_input else ""
        table.add_row(marker, action.label + suffix, action.command)

    table.add_row("[bold cyan][q][/bold cyan]", "Quit", "[dim]exit menu[/dim]")
    console_.print()
    console_.print(table)


def _prompt_choice(num_actions: int) -> str:
    """Prompt the user for a choice. Returns the raw string (digit or 'q')."""
    choices = [str(i) for i in range(1, num_actions + 1)] + ["q"]
    return Prompt.ask(
        "\n[bold]Pick a step[/bold]",
        choices=choices,
        default="1",
        show_choices=False,
    )


def _bin_name() -> str:
    """Return the binary name the operator used (``mdk`` or ``movate``).

    Lets shelled-out subcommands preserve the alias — if the user
    typed ``movate menu``, child invocations also use ``movate``.
    """
    basename = os.path.basename(sys.argv[0]) if sys.argv else "mdk"
    return "movate" if basename == "movate" else "mdk"


def _execute_action(action, *, dry_run: bool = False) -> int:
    """Shell out to the selected action's command.

    Uses the parent binary name so ``mdk menu`` → ``mdk <subcmd>``,
    ``movate menu`` → ``movate <subcmd>``. Inherits stdio so the
    subprocess output appears inline, identical to running directly.

    Returns the subprocess exit code. In ``dry_run`` mode just prints
    the command and returns 0 — used by tests and the future
    ``mdk menu --dry-run`` flag.
    """
    bin_ = _bin_name()
    full_cmd = (bin_, *action.argv)

    if dry_run:
        console.print(f"[dim]would run:[/dim] [bold]{' '.join(full_cmd)}[/bold]")
        return 0

    console.print(f"\n[dim]→ running:[/dim] [bold]{' '.join(full_cmd)}[/bold]\n")
    try:
        result = subprocess.run(full_cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        err_console.print(f"[red]✗[/red] could not find {bin_!r} on PATH")
        return 127


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def menu(
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root directory (default: current working directory).",
        hidden=True,
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "List status + suggestions without prompting. "
            "Useful in scripts (e.g. CI status reports) and for tests."
        ),
    ),
    auto: int = typer.Option(
        0,
        "--auto",
        help=(
            "Skip the prompt and select the Nth menu entry automatically. "
            "1 = top suggestion. Mostly for tests and shell aliases."
        ),
        hidden=True,
    ),
) -> None:
    """Show workspace status and a contextual list of next steps.

    [bold]Examples:[/bold]

      [dim]$ mdk menu                # interactive[/dim]
      [dim]$ mdk menu --dry-run      # show status + suggestions, no prompt[/dim]

    Each menu entry maps to a real [bold]mdk[/bold] subcommand —
    pick one and the menu shells out to it, preserving stdio so
    the output matches running directly. Add [bold]--help[/bold]
    after any subcommand name shown in the menu to learn more.
    """
    status = inspect_workspace(project_root)
    _render_status(status)

    actions = build_actions(status)
    _render_actions(actions)

    if dry_run:
        # Non-interactive: just show the panel + suggestions and exit.
        raise typer.Exit(code=0)

    if auto > 0:
        if auto > len(actions):
            err_console.print(
                f"[red]✗[/red] --auto {auto} but only {len(actions)} actions available"
            )
            raise typer.Exit(code=2)
        chosen = actions[auto - 1]
    else:
        choice = _prompt_choice(len(actions))
        if choice == "q":
            console.print("[dim]bye[/dim]")
            raise typer.Exit(code=0)
        chosen = actions[int(choice) - 1]

    if chosen.needs_user_input:
        # Don't auto-execute commands that need extra arguments
        # (e.g. agent name, run input). Print the template so the
        # operator can copy-paste / Tab-complete from there.
        # Same behavior whether reached via prompt or --auto.
        console.print(
            f"\n[yellow]ⓘ[/yellow] This action needs additional input.\n"
            f"  [bold]{chosen.command}[/bold]\n"
            f"[dim]Copy + adjust the command, then run it from your shell.[/dim]"
        )
        raise typer.Exit(code=0)

    exit_code = _execute_action(chosen)
    raise typer.Exit(code=exit_code)
