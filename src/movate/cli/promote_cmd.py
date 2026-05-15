"""``mdk promote <snap> --to <profile>`` — Sprint O Day 12-13.

The audit-trail half of the dev → staging → prod promotion flow.
A *promotion* records the decision "snapshot S is the canonical
state for profile P." The actual config restoration happens via
``mdk migrate`` or ``mdk rollback``; promote is what makes that
decision *legible* later.

  $ mdk snapshot create -d "release v0.7"           # take a snapshot
  $ mdk eval triage --gate 0.7                      # gate it
  $ mdk promote abc1234 --to staging                # record + audit
  $ mdk promote --list --profile staging            # see history
  $ mdk promote --current prod                      # what's deployed?

Eval gating is opt-in for MVP: pass ``--eval-pass-rate <float>``
with the score you observed from ``mdk eval``. A future enhancement
folds eval+promote into one atomic ``--require-eval <agent>`` flag.

Append-only by design — there is no ``unpromote``. Reverse a
mistake by promoting a different snapshot, which creates a new
log entry. The history shows what really happened.

[bold]Design call:[/bold] this is a single command with mode flags
rather than a Typer app with subcommands. Typer's callback + positional
+ subcommand combination is fragile when the positional arg looks
like a non-subcommand string; flat flags sidestep that entirely.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.profiles import load_registry
from movate.profiles.store import ProfileNotFoundError, ProfileStoreError
from movate.promotions import PromotionsStoreError, load_log
from movate.promotions.store import record_promotion
from movate.snapshot import (
    SnapshotNotFoundError,
    SnapshotStoreError,
    resolve_snapshot,
)

console = Console()
err_console = Console(stderr=True)


def _validate_profile(name: str) -> None:
    """Confirm ``name`` is a known profile. Typo-prevention only.

    The promotions log itself is permissive (accepts any string),
    but the CLI gates on the registry so operators can't promote
    to ``stagging`` because they fat-fingered ``staging``.
    """
    try:
        registry = load_registry()
        registry.get(name)
    except ProfileNotFoundError as exc:
        err_console.print(
            f"[red]✗[/red] {exc}\n[dim]known profiles: run [bold]mdk profiles list[/bold][/dim]"
        )
        raise typer.Exit(code=2) from None
    except ProfileStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None


# ---------------------------------------------------------------------------
# Mode dispatchers (factored for testability)
# ---------------------------------------------------------------------------


def _show_list(project_root: Path, profile_filter: str) -> None:
    """Print recorded promotions, optionally filtered by profile."""
    try:
        log = load_log(project_root)
    except PromotionsStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    entries = log.for_profile(profile_filter) if profile_filter else log.promotions
    if not entries:
        msg = (
            f"no promotions to [cyan]{profile_filter}[/cyan]"
            if profile_filter
            else "no promotions recorded yet"
        )
        console.print(
            f"[yellow]⚠[/yellow] {msg}. "
            f"Run [bold]mdk promote <snap> --to <profile>[/bold] to start."
        )
        return

    title = (
        f"Promotions to [cyan]{profile_filter}[/cyan] ({len(entries)})"
        if profile_filter
        else f"Promotions ({len(entries)})"
    )
    table = Table(title=title, title_style="bold")
    if not profile_filter:
        table.add_column("Profile", style="cyan", no_wrap=True)
    table.add_column("Snapshot", style="cyan", no_wrap=True)
    table.add_column("When", style="dim", no_wrap=True)
    table.add_column("By", style="dim", no_wrap=True)
    table.add_column("Eval", justify="right", style="dim")
    table.add_column("Note", style="dim")

    for entry in entries:
        eval_cell = f"{entry.eval_score:.2%}" if entry.eval_score is not None else "—"
        row = [
            entry.short_hash,
            entry.promoted_at,
            entry.promoted_by or "—",
            eval_cell,
            entry.description or "—",
        ]
        if not profile_filter:
            row.insert(0, entry.profile)
        table.add_row(*row)
    console.print(table)


def _show_current(project_root: Path, profile: str) -> None:
    """Print the most recent promotion to ``profile`` (exit 1 if none)."""
    try:
        log = load_log(project_root)
    except PromotionsStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    promotion = log.current(profile)
    if not promotion:
        console.print(
            f"[yellow]⚠[/yellow] no promotion recorded for profile [cyan]{profile}[/cyan]"
        )
        raise typer.Exit(code=1)

    body = (
        f"[bold]snapshot:[/bold]   [cyan]{promotion.short_hash}[/cyan]\n"
        f"[bold]promoted:[/bold]   {promotion.promoted_at}"
    )
    if promotion.promoted_by:
        body += f"\n[bold]by:[/bold]         {promotion.promoted_by}"
    if promotion.description:
        body += f"\n[bold]note:[/bold]       {promotion.description}"
    if promotion.eval_score is not None:
        body += f"\n[bold]eval:[/bold]       {promotion.eval_score:.2%}"
    console.print(
        Panel(
            body,
            title=f"Current promotion → [cyan]{profile}[/cyan]",
            title_align="left",
            border_style="cyan",
        )
    )


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def promote(  # noqa: PLR0912 — branch count is inherent to a multi-mode dispatcher
    snap: str = typer.Argument(
        "",
        help=(
            "Snapshot hash or prefix to promote. "
            "Omit when using [bold]--list[/bold] or [bold]--current[/bold]. "
            "Run [bold]mdk snapshot list[/bold] to browse snapshots."
        ),
        metavar="SNAP",
    ),
    to: str = typer.Option(
        "",
        "--to",
        "-t",
        help="Destination profile (e.g. [bold]staging[/bold], [bold]prod[/bold]).",
        metavar="PROFILE",
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="Operator note recorded with the promotion (e.g. release tag, ticket).",
    ),
    eval_pass_rate: float | None = typer.Option(
        None,
        "--eval-pass-rate",
        help=(
            "Observed eval pass rate (0.0-1.0) for the audit trail. "
            "Operators run [bold]mdk eval[/bold] separately and supply the score."
        ),
    ),
    list_mode: bool = typer.Option(
        False,
        "--list",
        help=("Show recorded promotions. Combine with [bold]--profile <name>[/bold] to filter."),
    ),
    current_mode: str = typer.Option(
        "",
        "--current",
        help=("Show the most recent promotion to PROFILE (e.g. [dim]--current prod[/dim])."),
        metavar="PROFILE",
    ),
    profile_filter: str = typer.Option(
        "",
        "--profile",
        "-p",
        help="Filter [bold]--list[/bold] output to a single profile.",
        metavar="PROFILE",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root (default: cwd).",
        hidden=True,
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be recorded without writing the log.",
    ),
) -> None:
    """Record a snapshot's promotion to a profile.

    [bold]Examples:[/bold]

      [dim]$ mdk promote abc1234 --to staging[/dim]
      [dim]$ mdk promote abc1234 --to staging --dry-run[/dim]
      [dim]$ mdk promote abc1234 --to prod -d "v0.7 release"[/dim]
      [dim]$ mdk promote abc1234 --to prod --eval-pass-rate 0.85[/dim]
      [dim]$ mdk promote --list[/dim]
      [dim]$ mdk promote --list --profile prod[/dim]
      [dim]$ mdk promote --current prod[/dim]
    """
    root = Path(project_root).resolve()

    # Mode: --current <profile>
    if current_mode:
        _show_current(root, current_mode)
        return

    # Mode: --list (optionally filtered)
    if list_mode:
        _show_list(root, profile_filter)
        return

    # Default mode: record a promotion. Validate required args.
    if not snap:
        err_console.print(
            "[red]✗[/red] missing required argument [bold]SNAP[/bold]. "
            "[dim]Run [bold]mdk promote --help[/bold] for usage.[/dim]"
        )
        raise typer.Exit(code=2)
    if not to:
        err_console.print("[red]✗[/red] missing required option [bold]--to PROFILE[/bold].")
        raise typer.Exit(code=2)
    if eval_pass_rate is not None and not (0.0 <= eval_pass_rate <= 1.0):
        err_console.print(
            f"[red]✗[/red] --eval-pass-rate must be in [0.0, 1.0]; got {eval_pass_rate}"
        )
        raise typer.Exit(code=2)

    # Validate the target profile exists (typo prevention).
    _validate_profile(to)

    # Resolve the snapshot (hash/prefix → full manifest).
    try:
        manifest = resolve_snapshot(root, snap)
    except SnapshotNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SnapshotStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    short = manifest.hash.removeprefix("sha256:")[:8]

    if dry_run:
        body = (
            f"[bold]snapshot:[/bold]   [cyan]{short}[/cyan]\n"
            f"[bold]→ profile:[/bold]  [cyan]{to}[/cyan]"
        )
        if description:
            body += f"\n[bold]note:[/bold]      {description}"
        if eval_pass_rate is not None:
            body += f"\n[bold]eval:[/bold]      {eval_pass_rate:.2%}"
        console.print(
            Panel(
                body + "\n\n[yellow]⚠ dry-run — no write.[/yellow]",
                title="Would record promotion",
                title_align="left",
                border_style="yellow",
            )
        )
        return

    try:
        promotion = record_promotion(
            project_root=root,
            profile=to,
            snapshot_hash=manifest.hash,
            description=description,
            eval_score=eval_pass_rate,
        )
    except PromotionsStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    body = (
        f"[bold]snapshot:[/bold]   [cyan]{promotion.short_hash}[/cyan]\n"
        f"[bold]→ profile:[/bold]  [cyan]{promotion.profile}[/cyan]\n"
        f"[bold]at:[/bold]         {promotion.promoted_at}"
    )
    if promotion.promoted_by:
        body += f"\n[bold]by:[/bold]         {promotion.promoted_by}"
    if promotion.description:
        body += f"\n[bold]note:[/bold]       {promotion.description}"
    if promotion.eval_score is not None:
        body += f"\n[bold]eval:[/bold]       {promotion.eval_score:.2%}"
    console.print(
        Panel(
            body,
            title="[green]✓[/green] Promotion recorded",
            title_align="left",
            border_style="green",
        )
    )
