"""``mdk fix`` — auto-remediate common diagnostic findings (Sprint P).

The repair-side companion to ``mdk doctor`` (which diagnoses but
never modifies). Same UX convention as ``mdk fmt`` / ``mdk migrate``:
default is ``--dry-run`` (preview), operators opt into writes with
``--apply``.

  $ mdk doctor             # diagnose
  $ mdk fix                # preview what would be repaired
  $ mdk fix --apply        # apply the fixes

Bundled fixes for MVP:

* ``ensure-movate-dir`` — create ``.movate/``
* ``ensure-gitignore`` — create ``.gitignore`` with movate-aware ignores
* ``ensure-env-from-example`` — ``cp .env.example .env``
* ``ensure-agents-dir`` — create empty ``agents/.gitkeep``
* ``fix-secrets-permissions`` — chmod 0600 on ``~/.movate/secrets/*.yaml``
* ``unshadow-runtime-keys`` — comment a stale ``export <VAR>=...`` shell-profile
  line that shadows a saved key in ``~/.movate/credentials``

Each fix is idempotent — re-running on a clean tree is a no-op.

[bold]Design call vs the BACKLOG:[/bold] the BACKLOG slots this as
``mdk doctor fix``. We ship as ``mdk fix`` instead because it matches
the verb-style convention (``run``, ``eval``, ``fmt``, ``demo``,
``init``, ``snapshot``, ``promote``...). ``doctor`` stays unchanged
(a noun for "diagnostic snapshot"). The two commands are linked
explicitly in each other's help text.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.fixes import FixStatus, available_fixes, diagnose_and_fix

console = Console()
err_console = Console(stderr=True)


_STATUS_RENDERING = {
    FixStatus.NOT_NEEDED: ("[dim]·[/dim]", "[dim]not needed[/dim]"),
    FixStatus.WOULD_APPLY: ("[yellow]⚠[/yellow]", "[yellow]would apply[/yellow]"),
    FixStatus.APPLIED: ("[green]✓[/green]", "[green]applied[/green]"),
    FixStatus.FAILED: ("[red]✗[/red]", "[red]failed[/red]"),
}


def fix(
    apply_: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Actually write the changes. Default is a [bold]--dry-run[/bold] "
            "preview — pass [bold]--apply[/bold] to commit."
        ),
    ),
    only: list[str] = typer.Option(
        None,
        "--only",
        help=(
            "Run only the named fix(es). Repeatable: "
            "[dim]--only ensure-gitignore --only ensure-movate-dir[/dim]. "
            "Use [bold]mdk fix --list[/bold] to see all fix ids."
        ),
    ),
    skip: list[str] = typer.Option(
        None,
        "--skip",
        help="Skip the named fix(es). Repeatable. Mutually wins-against --only.",
    ),
    list_: bool = typer.Option(
        False,
        "--list",
        help=(
            "List all available fixes (id + label + description) and exit. No diagnosis, no writes."
        ),
    ),
    explain: str = typer.Option(
        "",
        "--explain",
        help=(
            "Print the full description + current applicability of one fix "
            "and exit. Example: [dim]--explain ensure-gitignore[/dim]."
        ),
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd).",
        hidden=True,
    ),
) -> None:
    """Auto-remediate common diagnostic findings.

    Default is a dry-run preview. Add [bold]--apply[/bold] to actually
    write changes. Use [bold]mdk doctor[/bold] to diagnose first if
    you want the full picture before committing to a fix.

    [bold]Examples:[/bold]

      [dim]$ mdk fix                                # preview all fixes[/dim]
      [dim]$ mdk fix --apply                        # write all fixes[/dim]
      [dim]$ mdk fix --only ensure-gitignore --apply  # just one fix[/dim]
      [dim]$ mdk fix --list                         # show fix ids + descriptions[/dim]
    """
    if list_:
        _render_fix_catalog()
        return

    if explain:
        _render_fix_explain(explain, project_root=Path(project_root).resolve())
        return

    root = Path(project_root).resolve()
    dry_run = not apply_

    only_t = tuple(only or ())
    skip_t = tuple(skip or ())

    results = diagnose_and_fix(root, dry_run=dry_run, only=only_t, skip=skip_t)

    # Render the per-fix table.
    table = Table(
        title="mdk fix" + (" — dry-run preview" if dry_run else ""),
        title_style="bold",
        show_lines=False,
    )
    table.add_column("", no_wrap=True)
    table.add_column("Fix", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", style="dim")

    for result in results:
        marker, status_text = _STATUS_RENDERING[result.status]
        table.add_row(marker, result.fix_id, status_text, result.message or "")
    console.print(table)

    # Summary line + exit-code logic.
    n_applied = sum(1 for r in results if r.status is FixStatus.APPLIED)
    n_would = sum(1 for r in results if r.status is FixStatus.WOULD_APPLY)
    n_failed = sum(1 for r in results if r.status is FixStatus.FAILED)
    n_skipped = sum(1 for r in results if r.status is FixStatus.NOT_NEEDED)

    if dry_run:
        if n_would == 0:
            console.print(f"\n[green]✓[/green] nothing to fix — {n_skipped} check(s) passed clean.")
            return
        console.print(
            f"\n[yellow]⚠[/yellow] {n_would} fix(es) would apply, "
            f"{n_skipped} already clean. "
            "Re-run with [bold]--apply[/bold] to write."
        )
        return

    # --apply mode
    if n_failed > 0:
        console.print(
            f"\n[red]✗[/red] {n_applied} fix(es) applied, {n_failed} failed, "
            f"{n_skipped} not needed."
        )
        raise typer.Exit(code=1)
    if n_applied == 0:
        console.print(f"\n[green]✓[/green] nothing to fix — {n_skipped} check(s) already clean.")
        return
    console.print(f"\n[green]✓[/green] {n_applied} fix(es) applied, {n_skipped} already clean.")


def _render_fix_explain(fix_id: str, *, project_root: Path) -> None:
    """Print one fix's full description + whether it currently applies.

    Operators reach for ``--explain`` when ``--list`` showed an entry
    they don't recognize. Shows the label, description, AND runs the
    fix's ``check()`` so the answer to "would this fire here?" comes
    in the same view.
    """
    candidates = [f for f in available_fixes() if f.id == fix_id]
    if not candidates:
        valid = ", ".join(f.id for f in available_fixes())
        err_console.print(
            f"[red]✗[/red] unknown fix [bold]{fix_id}[/bold]. [dim]Valid ids: {valid}.[/dim]"
        )
        raise typer.Exit(code=2)
    fix_def = candidates[0]
    applies = fix_def.check(project_root)
    body = (
        f"[bold]ID:[/bold]          [cyan]{fix_def.id}[/cyan]\n"
        f"[bold]Label:[/bold]       {fix_def.label}\n\n"
        f"{fix_def.description}\n\n"
        f"[bold]Applies here?[/bold] "
        + (
            f"[yellow]yes[/yellow] — run [cyan]mdk fix --only {fix_def.id} --apply[/cyan] to apply"
            if applies
            else "[green]no[/green] — your project is already clean for this check"
        )
    )
    console.print(
        Panel(
            body,
            title=f"Fix: [cyan]{fix_def.id}[/cyan]",
            title_align="left",
            border_style="cyan",
        )
    )


def _render_fix_catalog() -> None:
    """Print the available-fix catalog as a reference table."""
    table = Table(title="Available fixes", title_style="bold", show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Label", no_wrap=True)
    table.add_column("Description", style="dim")
    for f in available_fixes():
        table.add_row(f.id, f.label, f.description)
    console.print(table)
    console.print(
        Panel(
            "[bold]Use:[/bold] [cyan]mdk fix --only <id>[/cyan] to run a single fix, or "
            "[cyan]mdk fix[/cyan] (no flag) to preview all.\n\n"
            "Diagnose first with [cyan]mdk doctor[/cyan] for a full environment check.",
            border_style="dim",
        )
    )
