"""``mdk docs`` — auto-generated project documentation (Sprint P).

Subcommands:

* ``runbook`` — generate an ops-friendly markdown runbook from
  the project's current state. Includes agents, env vars, common
  recipes, troubleshooting, and a state-cluster cheatsheet.
* ``cli`` — generate a complete CLI command reference (the command
  tree plus each command's help + options) by introspecting the
  Typer app's underlying Click command tree. Pure introspection —
  no project state, no network.

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
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel

from movate.docs import build_context, generate_runbook

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# CLI reference generation (introspection-only — importable from scripts/)
# ---------------------------------------------------------------------------


def _is_group(command: Any) -> bool:
    """True if ``command`` is a Click group (has discoverable subcommands).

    The ``mdk`` root + its sub-apps are ``TyperGroup`` subclasses, which
    are NOT ``click.Group`` instances — so duck-type on ``commands``
    rather than ``isinstance``.
    """
    return hasattr(command, "commands") and bool(getattr(command, "commands", None) is not None)


def _walk(
    command: Any,
    *,
    path: str,
    depth: int,
    tree: list[str],
    leaves: list[tuple[str, Any]],
) -> None:
    """Depth-first walk of the Click command tree.

    Appends an indented tree bullet for every command and records each
    command (group or leaf) in ``leaves`` so the caller can render its
    ``--help`` section. Hidden commands are skipped — they're not part of
    the advertised surface. Subcommands are visited in sorted order so the
    output is deterministic (stable diffs in CI).
    """
    if depth > 0:  # don't list the root in the tree bullets
        short = (command.get_short_help_str(limit=80) or "").strip()
        indent = "  " * (depth - 1)
        tree.append(f"{indent}- `{path}`{' — ' + short if short else ''}")

    leaves.append((path, command))

    if _is_group(command):
        for name in sorted(command.commands):
            sub = command.commands[name]
            if getattr(sub, "hidden", False):
                continue
            _walk(sub, path=f"{path} {name}", depth=depth + 1, tree=tree, leaves=leaves)


def _help_text(path: str, command: Any) -> str:
    """Capture ``command``'s ``--help`` output as plain text.

    Typer renders help via Rich straight to a console (so ``get_help``
    returns an empty string), so we run the command through Click's test
    runner with a fixed terminal width and strip ANSI. This mirrors
    exactly what a user sees from ``<path> --help``.
    """
    import os  # noqa: PLC0415

    from click.testing import CliRunner  # noqa: PLC0415

    # Pin width so wrapping is deterministic across machines/CI.
    runner = CliRunner()
    result = runner.invoke(
        command,
        ["--help"],
        prog_name=path,
        env={**os.environ, "COLUMNS": "100", "TERM": "dumb", "NO_COLOR": "1"},
        color=False,
    )
    # Rich pads every line to the terminal width; strip the trailing
    # whitespace so the committed doc is clean (and trailing-whitespace
    # linters stay quiet).
    return "\n".join(line.rstrip() for line in result.output.splitlines())


def generate_cli_reference(*, prog_name: str = "mdk") -> str:
    """Render the full ``mdk`` CLI reference as GitHub-flavored markdown.

    Introspects the Typer app via its underlying Click command tree, so
    the output stays in lockstep with the real commands + options. Pure
    function — no filesystem writes, no network, no project state — which
    is what lets both ``mdk docs cli`` and ``scripts/gen_cli_reference.py``
    share one implementation.
    """
    import typer  # noqa: PLC0415

    # Import lazily so this module can be imported (e.g. by scripts) without
    # forcing the full CLI import graph at module load.
    from movate.cli.main import app as cli_app  # noqa: PLC0415

    root = typer.main.get_command(cli_app)
    # Click derives the prog name from sys.argv otherwise; pin it so the
    # reference reads as ``mdk ...`` regardless of how it was invoked.
    root.name = prog_name

    tree: list[str] = []
    leaves: list[tuple[str, Any]] = []
    _walk(root, path=prog_name, depth=0, tree=tree, leaves=leaves)

    lines: list[str] = []
    lines.append(f"# `{prog_name}` CLI reference\n")
    lines.append(
        "Auto-generated by `mdk docs cli` (or `scripts/gen_cli_reference.py`). "
        "Do not edit by hand — regenerate after changing commands.\n"
    )
    lines.append("## Command tree\n")
    lines.extend(tree)
    lines.append("")
    lines.append("## Commands\n")
    for path, command in leaves:
        lines.append(f"### `{path}`\n")
        lines.append("```")
        lines.append(_help_text(path, command))
        lines.append("```\n")

    return "\n".join(lines).rstrip() + "\n"


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


# ---------------------------------------------------------------------------
# Subcommand: cli
# ---------------------------------------------------------------------------


@docs_app.command("cli")
def cli(
    output: Path = typer.Argument(
        Path("docs/cli-reference.md"),
        help="Where to write the reference (default: docs/cli-reference.md).",
        metavar="OUTPUT",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help=(
            "CI mode: regenerate in memory and exit non-zero if OUTPUT is "
            "stale (or missing). Writes nothing."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the generated markdown to stdout without writing.",
    ),
) -> None:
    """Generate the full ``mdk`` CLI command reference.

    Introspects the Typer app's underlying Click command tree and emits a
    markdown doc: the command tree, then each command's help + options.
    Pure introspection — no project state, no network — so it's safe in CI.

    Use ``--check`` in CI to fail the build if the committed reference has
    drifted from the live command surface.

    [bold]Examples:[/bold]

      [dim]$ mdk docs cli                       # writes docs/cli-reference.md[/dim]
      [dim]$ mdk docs cli --dry-run             # preview, no write[/dim]
      [dim]$ mdk docs cli --check               # CI freshness gate[/dim]
    """
    markdown = generate_cli_reference()

    if dry_run:
        typer.echo(markdown, nl=False)
        return

    target = output if output.is_absolute() else (Path.cwd() / output)
    target = target.resolve()

    if check:
        current = target.read_text() if target.exists() else None
        if current == markdown:
            console.print(f"[green]✓[/green] {target} is up to date")
            return
        err_console.print(
            f"[red]✗[/red] {target} is stale — run [bold]mdk docs cli[/bold] and commit the result"
        )
        raise typer.Exit(code=1)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown)
    console.print(
        Panel(
            f"[bold]Wrote:[/bold] [cyan]{target}[/cyan]",
            title="[green]✓[/green] CLI reference generated",
            title_align="left",
            border_style="green",
        )
    )
