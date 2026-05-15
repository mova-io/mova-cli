"""``mdk migrate <snap> [--filter GLOB] [--dry-run] [--apply]`` — Sprint O Day 10-11.

Surgical per-file restoration from a snapshot into the current workspace.
Distinct from ``mdk rollback``:

* **rollback** restores ALL files and always auto-creates a pre-rollback
  snapshot (it's the "undo everything" hammer).
* **migrate** is the scalpel — you pick exactly which files to bring
  forward, it defaults to a preview (:option:`--dry-run`), and
  :option:`--backup` is opt-in rather than mandatory.

Typical flows::

  # Preview what would change (always safe — default --dry-run):
  $ mdk migrate abc1234

  # Restore only the triage agent from last week's snapshot:
  $ mdk migrate abc1234 --filter 'agents/triage/**' --apply

  # Migrate all agents from a staging snapshot, snapshot current state first:
  $ mdk migrate abc1234 --filter 'agents/**' --backup --apply

  # Non-interactive (CI):
  $ mdk migrate abc1234 --apply --yes

The ``--filter`` glob is matched against each file's **relative path**
(e.g. ``agents/triage/agent.yaml``) using :func:`fnmatch.fnmatch`.
Pass ``--filter 'agents/**'`` to restore all agents, or
``--filter 'agents/triage/**'`` for a single agent's files.
Without ``--filter`` every file in the snapshot is a candidate.
"""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.snapshot import (
    SnapshotManifest,
    SnapshotNotFoundError,
    SnapshotStoreError,
    create_snapshot,
    resolve_snapshot,
    snapshot_path,
)
from movate.snapshot.manifest import FileEntry

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_files(
    files: tuple[FileEntry, ...],
    pattern: str | None,
) -> list[FileEntry]:
    """Return files matching ``pattern`` (or all files if pattern is None).

    ``fnmatch.fnmatch`` handles simple shell patterns: ``*``, ``?``,
    ``[seq]``, ``**`` (treated literally by fnmatch — use ``agents/*``
    not ``agents/**`` for a single-level match, or rely on the fact
    that ``agents/**`` matches ``agents/foo/bar.yaml`` because fnmatch
    treats ``**`` as ``*``).
    """
    if pattern is None:
        return list(files)
    return [f for f in files if fnmatch.fnmatch(f.path, pattern)]


def _restore_files(
    *,
    manifest: SnapshotManifest,
    files: list[FileEntry],
    project_root: Path,
) -> list[str]:
    """Copy ``files`` from the snapshot store into ``project_root``.

    Returns a list of restored relative paths for the confirmation
    summary. Raises :exc:`typer.Exit` (code 2) on IO failure.
    """
    short = manifest.hash.removeprefix("sha256:")[:8]
    snap_files_dir = snapshot_path(project_root, short) / "files"
    if not snap_files_dir.is_dir():
        err_console.print(
            f"[red]✗[/red] snapshot {short!r} has no files/ directory at "
            f"{snap_files_dir} — store corruption?"
        )
        raise typer.Exit(code=2)

    restored: list[str] = []
    for entry in files:
        src = snap_files_dir / entry.path
        dst = project_root / entry.path
        if not src.is_file():
            err_console.print(
                f"[red]✗[/red] captured file {entry.path!r} missing from snapshot store at {src}"
            )
            raise typer.Exit(code=2)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(entry.path)
    return restored


def _render_preview(
    files: list[FileEntry],
    manifest: SnapshotManifest,
    *,
    dry_run: bool,
) -> None:
    """Render a Rich table listing files that will be (or would be) restored."""
    short = manifest.hash.removeprefix("sha256:")[:8]
    header = (
        f"[yellow]Dry-run preview[/yellow] — snapshot [cyan]{short}[/cyan]"
        if dry_run
        else f"Restoring from snapshot [cyan]{short}[/cyan]"
    )
    table = Table(title=header, title_style="bold", show_lines=False)
    table.add_column("File", style="cyan", no_wrap=True)
    table.add_column("Size", justify="right", style="dim")
    for f in files:
        table.add_row(f.path, f"{f.size:,} B")
    console.print(table)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def migrate(
    snap: str = typer.Argument(
        ...,
        help=(
            "Snapshot hash or prefix to migrate from. "
            "Run [bold]mdk snapshot list[/bold] to browse available snapshots."
        ),
        metavar="SNAP",
    ),
    filter_: str | None = typer.Option(
        None,
        "--filter",
        "-f",
        help=(
            "Shell glob applied to each file's relative path. "
            "Examples: [dim]--filter 'agents/**'[/dim] or "
            "[dim]--filter 'agents/triage/**'[/dim]. "
            "Omit to migrate every file in the snapshot."
        ),
        metavar="GLOB",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help=(
            "[bold]--dry-run[/bold] (default): preview changes without writing. "
            "[bold]--apply[/bold]: actually restore files to the workspace."
        ),
    ),
    backup: bool = typer.Option(
        False,
        "--backup/--no-backup",
        help=(
            "Auto-snapshot the current workspace before applying changes. "
            "Only meaningful when used with [bold]--apply[/bold]; ignored in "
            "dry-run mode."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt (non-interactive / CI).",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        help="Project root directory (default: current working directory).",
        hidden=True,
    ),
) -> None:
    """Restore files from a snapshot into the current workspace.

    Defaults to a safe [bold]--dry-run[/bold] preview — add
    [bold]--apply[/bold] to actually write files. Use
    [bold]--filter 'agents/**'[/bold] to scope the restore to specific
    files, or omit [bold]--filter[/bold] to restore everything in the
    snapshot.

    [bold]Examples:[/bold]

      [dim]$ mdk migrate abc1234                              # preview[/dim]
      [dim]$ mdk migrate abc1234 --apply                      # restore all[/dim]
      [dim]$ mdk migrate abc1234 --filter 'agents/**' --apply # agents only[/dim]
      [dim]$ mdk migrate abc1234 --backup --apply --yes       # CI-safe[/dim]
    """
    root = Path(project_root).resolve()

    # 1. Resolve the snapshot.
    try:
        manifest = resolve_snapshot(root, snap)
    except SnapshotNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SnapshotStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    short = manifest.hash.removeprefix("sha256:")[:8]

    # 2. Filter files.
    candidates = _filter_files(manifest.files, filter_)
    if not candidates:
        filter_hint = f" matching {filter_!r}" if filter_ else ""
        console.print(f"[yellow]⚠[/yellow] no files{filter_hint} in snapshot [cyan]{short}[/cyan]")
        raise typer.Exit(code=0)

    # 3. Render preview.
    _render_preview(candidates, manifest, dry_run=dry_run)

    if dry_run:
        console.print(
            f"\n[yellow]⚠ dry-run:[/yellow] {len(candidates)} file(s) would be restored "
            f"from snapshot [cyan]{short}[/cyan].\n"
            f"Re-run with [bold]--apply[/bold] to write the files."
        )
        raise typer.Exit(code=0)

    # 4. Confirm (unless --yes).
    if not yes:
        confirmed = typer.confirm(
            f"Restore {len(candidates)} file(s) into {root} from snapshot {short}?",
            default=False,
        )
        if not confirmed:
            console.print("[dim]Aborted.[/dim]")
            raise typer.Exit(code=0)

    # 5. Optional pre-migrate backup.
    if backup:
        try:
            pre = create_snapshot(
                project_root=root,
                description=f"pre-migrate backup (migrating FROM {short})",
                extras={"migrate_source": manifest.hash},
            )
            pre_short = pre.hash.removeprefix("sha256:")[:8]
            console.print(
                f"[green]✓[/green] backed up current state → snapshot [cyan]{pre_short}[/cyan]"
            )
        except SnapshotStoreError as exc:
            err_console.print(f"[red]✗[/red] backup failed: {exc}")
            raise typer.Exit(code=2) from None

    # 6. Restore.
    restored = _restore_files(manifest=manifest, files=candidates, project_root=root)

    # 7. Confirmation summary.
    console.print(
        Panel(
            "\n".join(f"  [cyan]{p}[/cyan]" for p in restored),
            title=(
                f"[green]✓[/green] migrated {len(restored)} file(s) from "
                f"snapshot [cyan]{short}[/cyan]"
            ),
            title_align="left",
            border_style="green",
        )
    )
    if manifest.description:
        console.print(f"[dim]snapshot note: {manifest.description}[/dim]")
