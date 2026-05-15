"""``mdk rollback <hash>`` — restore a snapshot (Sprint N Day 6-7).

Pairs with ``mdk snapshot`` (Day 1-3) + ``mdk diff`` (Day 4-5) to
complete the local state-cluster surface. Restores the project to
the state captured by a prior snapshot.

**Never destructive.** Before restoring, ``mdk rollback`` auto-
captures a "pre-rollback" snapshot of current state so the operator
can always re-roll forward. The audit trail reads like git:

  $ mdk snapshot list
  abc12345  rolled back FROM def67890
  def67890  red-bar after model swap
  9a8b7c6d  green deploy

  $ mdk rollback 9a8b7c6d        # roll back to green deploy
  $ mdk rollback abc12345        # ...or roll FORWARD to before the rollback

The ``--force`` gate matches ``mdk snapshot delete`` — read-only
flows (list/show/diff) need no confirmation; mutating flows
(rollback, delete) require explicit opt-in.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from movate.snapshot import (
    SnapshotNotFoundError,
    SnapshotRollbackError,
    SnapshotStoreError,
    rollback_to,
)
from movate.snapshot.store import resolve_snapshot

console = Console()
err_console = Console(stderr=True)


def _resolve_project_root(explicit: Path | None) -> Path:
    """Walk-up resolution — same convention as snapshot_cmd / diff_cmd."""
    if explicit is not None:
        if not explicit.is_dir():
            err_console.print(f"[red]✗[/red] --project path is not a directory: {explicit}")
            raise typer.Exit(code=2)
        return explicit.resolve()
    current = Path.cwd().resolve()
    while True:
        if (current / "movate.yaml").is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return Path.cwd().resolve()


def rollback(
    hash_or_prefix: str = typer.Argument(
        ...,
        help=(
            "Snapshot hash or short-hash prefix to restore. "
            "Run [bold]mdk snapshot list[/bold] to see available hashes."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Required to actually perform the rollback. The default "
            "behavior is a [bold]dry-run preview[/bold] that shows "
            "what would be restored. Rollback is auto-captured (a "
            "pre-rollback snapshot is created first) but it still "
            "rewrites files in the working tree — requiring --force "
            "matches the [bold]mdk snapshot delete[/bold] safety gate."
        ),
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root. Defaults to walking up from cwd.",
    ),
) -> None:
    """Restore the project to a prior snapshot.

    [bold]Defaults to dry-run.[/bold] Run with ``--force`` to actually
    apply. Before applying, an automatic snapshot of the CURRENT state
    is captured — the rollback is itself a state-graph operation, not
    a destructive mutation. The pre-rollback snapshot's description
    encodes the rollback origin (``rolled back FROM <hash>``) for the
    audit trail.

    [bold]Examples:[/bold]

      [dim]# Preview what rollback would restore[/dim]
      $ mdk rollback abc12345

      [dim]# Actually roll back[/dim]
      $ mdk rollback abc12345 --force

      [dim]# Roll forward — restore the auto-created pre-rollback snapshot[/dim]
      $ mdk snapshot list   # find the pre-rollback hash
      $ mdk rollback <pre-rollback-hash> --force

    [bold]Files that aren't in the target snapshot are left alone[/bold] —
    rollback restores captured files, not "everything the snapshot
    didn't know about." This preserves operator-edited scratch files
    outside the capture-root whitelist (movate.yaml, agents/, ...).
    """
    project_root = _resolve_project_root(project)

    if not force:
        # Dry-run: resolve target + show what would happen.
        try:
            target = resolve_snapshot(project_root, hash_or_prefix)
        except SnapshotNotFoundError as exc:
            err_console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=1) from None
        except SnapshotStoreError as exc:
            err_console.print(f"[red]✗[/red] {exc}")
            raise typer.Exit(code=2) from None

        short = target.hash.removeprefix("sha256:")[:8]
        body = (
            f"[bold]target:[/bold]   [cyan]{short}[/cyan] "
            f"[dim]({len(target.files)} files, {target.agent_count} agent(s))[/dim]\n"
            f"[bold]captured:[/bold] {target.created_at}\n"
        )
        if target.description:
            body += f"[bold]desc:[/bold]     {target.description}\n"
        body += (
            "\n[yellow]⚠ dry-run[/yellow] — no files modified.\n"
            "Re-run with [bold]--force[/bold] to apply the rollback.\n"
            "A pre-rollback snapshot will be auto-captured first."
        )
        console.print(
            Panel(
                body,
                title="Would roll back",
                title_align="left",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=1)

    # --force path: do it.
    try:
        result = rollback_to(project_root=project_root, target_hash=hash_or_prefix)
    except SnapshotNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except (SnapshotRollbackError, SnapshotStoreError) as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    target_short = result.target.hash.removeprefix("sha256:")[:8]
    pre_short = result.pre_snapshot.hash.removeprefix("sha256:")[:8]

    body = (
        f"[bold]restored:[/bold]  {result.restored_count} file(s) from "
        f"[cyan]{target_short}[/cyan]\n"
        f"[bold]pre-snap:[/bold]  [cyan]{pre_short}[/cyan] "
        f"[dim](current state captured before rollback)[/dim]\n"
    )
    if result.target.description:
        body += f"[bold]target:[/bold]    {result.target.description}\n"
    body += (
        f"\n[dim]To roll forward (undo this rollback):[/dim]\n"
        f"  [cyan]mdk rollback {pre_short} --force[/cyan]"
    )

    console.print(
        Panel(
            body,
            title="✓ Rolled back",
            title_align="left",
            border_style="green",
        )
    )
