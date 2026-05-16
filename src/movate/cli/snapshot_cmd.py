"""``mdk snapshot {create, list, show, delete}`` — state cluster Day 1-3.

First member of the K-state cluster (BACKLOG Group K Tier 0). Snapshot
is the central operational primitive per the Group K North Star —
every other state command (``diff``, ``rollback``, ``promote``, ``audit``,
``migrate``) operates on snapshots.

  $ mdk snapshot create --description "green deploy before model swap"
  $ mdk snapshot list
  $ mdk snapshot show abc12345
  $ mdk snapshot delete abc12345 --force
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.cli._resolve import walk_up_for_project_root
from movate.snapshot import (
    SnapshotNotFoundError,
    SnapshotStoreError,
    create_snapshot,
    delete_snapshot,
    list_snapshots,
)
from movate.snapshot.store import resolve_snapshot

console = Console()
err_console = Console(stderr=True)


snapshot_app = typer.Typer(
    name="snapshot",
    help=(
        "Capture / compare / restore project state. Snapshots are "
        "[bold]immutable + content-addressed[/bold] (git-style hash); two "
        "snapshots of identical state collide deterministically. "
        "Pairs with [bold]mdk diff[/bold], [bold]rollback[/bold], "
        "[bold]promote[/bold] (next in Sprint N)."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _resolve_project_root(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_dir():
            err_console.print(f"[red]✗[/red] --project path is not a directory: {explicit}")
            raise typer.Exit(code=2)
        return explicit.resolve()
    # Fall back to cwd — a fresh project that hasn't been `mdk init`'d
    # yet should still be snapshot-able.
    return walk_up_for_project_root() or Path.cwd().resolve()


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@snapshot_app.command("create")
def create(
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help=(
            "One-line description of what this snapshot represents. "
            "Surfaced in [bold]mdk snapshot list[/bold] + [bold]show[/bold]. "
            "Pick something searchable: 'green deploy before model swap', "
            "'staging baseline 2026-05-15', etc."
        ),
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root. Defaults to walking up from cwd for movate.yaml.",
    ),
) -> None:
    """Capture the current project state as an immutable snapshot.

    Captures: movate.yaml, policy.yaml, knowledge.yaml, and every
    file under agents/ + contexts/ + workflows/ + skills/. Each
    file is hashed (sha256); the snapshot's own hash is the SHA-256
    of the manifest. Idempotent — re-running over identical state
    returns the same hash + skips the disk write.

    Files live at [bold].movate/snapshots/<short-hash>/[/bold]:
      - manifest.yaml — the metadata
      - files/ — copy of captured project files

    Future operational commands operate on these snapshots:
      [dim]mdk diff <a> <b>     # what changed between two snapshots[/dim]
      [dim]mdk rollback <hash>  # restore a prior snapshot[/dim]
      [dim]mdk audit <hash>     # production-readiness scan[/dim]
    """
    project_root = _resolve_project_root(project)
    try:
        manifest = create_snapshot(
            project_root=project_root,
            description=description,
        )
    except SnapshotStoreError as exc:
        err_console.print(f"[red]✗[/red] snapshot failed: {exc}")
        raise typer.Exit(code=2) from None

    short = manifest.hash.removeprefix("sha256:")[:8]
    console.print(
        Panel(
            f"[bold]snapshot[/bold] [cyan]{short}[/cyan] "
            f"[dim]({len(manifest.files)} files, "
            f"{manifest.agent_count} agent(s))[/dim]\n\n"
            f"[bold]hash:[/bold] {manifest.hash}\n"
            f"[bold]when:[/bold] {manifest.created_at}\n"
            f"[bold]where:[/bold] {project_root}/.movate/snapshots/{short}/\n"
            + (f"[bold]desc:[/bold] {manifest.description}\n" if manifest.description else ""),
            title="✓ Created",
            title_align="left",
            border_style="green",
        )
    )
    console.print()
    console.print("[dim]Next steps:[/dim]")
    console.print(f"  [cyan]mdk snapshot show {short}[/cyan]    [dim]# inspect[/dim]")
    console.print("  [cyan]mdk snapshot list[/cyan]           [dim]# see all[/dim]")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@snapshot_app.command("list")
def list_(
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root. Defaults to walking up from cwd.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table — pipe-friendly.",
    ),
) -> None:
    """List every snapshot in the project's ``.movate/snapshots/`` dir.

    Newest first. Default rendering is a Rich table with the short
    hash, description, file count, and timestamp.
    """
    project_root = _resolve_project_root(project)
    manifests = list_snapshots(project_root)

    if json_output:
        payload = [m._as_serialisable() for m in manifests]
        console.print_json(json.dumps(payload, default=str))
        return

    if not manifests:
        console.print(
            "[yellow]⚠[/yellow] no snapshots yet. Create one with [bold]mdk snapshot create[/bold]."
        )
        return

    table = Table(
        title=f"Snapshots ({len(manifests)})",
        title_style="bold",
        show_lines=False,
    )
    table.add_column("Hash", style="cyan", no_wrap=True)
    # "Age" column reads like git-log's relative time — scannable on
    # a 30-row table where the absolute ISO timestamp is just noise.
    table.add_column("Age", style="dim", no_wrap=True)
    table.add_column("When", style="dim", no_wrap=True)
    table.add_column("Files", justify="right", style="dim", no_wrap=True)
    table.add_column("Agents", justify="right", style="dim", no_wrap=True)
    table.add_column("Description", style="white")

    for manifest in manifests:
        short = manifest.hash.removeprefix("sha256:")[:8]
        table.add_row(
            short,
            _relative_age(manifest.created_at),
            manifest.created_at,
            str(len(manifest.files)),
            str(manifest.agent_count),
            manifest.description or "[dim]—[/dim]",
        )
    console.print(table)


def _relative_age(iso_ts: str) -> str:
    """Render an ISO-8601 timestamp as 'N{s,m,h,d,w} ago'.

    Permissive — unparseable input renders as "—" so a corrupted
    manifest never blows up the list view. Matches the granularity
    operators expect from ``git log --format=%ar``:

      <60s   →  Xs
      <60m   →  Xm
      <24h   →  Xh
      <14d   →  Xd
      else   →  Xw
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    try:
        # The manifest stores `2026-05-15T14:00:00.123Z`. fromisoformat
        # accepts that shape if we drop the trailing 'Z' (single char,
        # not a multi-char suffix — keep ms precision intact).
        trimmed = iso_ts.removesuffix("Z")
        ts = datetime.fromisoformat(trimmed).replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        return "—"
    now = datetime.now(UTC)
    delta = (now - ts).total_seconds()
    if delta < 0:
        # Clock skew or test-injected future time — render as "now"
        # rather than a confusing "-3s".
        return "now"
    minute, hour, day, week = 60, 3600, 86400, 86400 * 7
    if delta < minute:
        return f"{int(delta)}s ago"
    if delta < hour:
        return f"{int(delta / minute)}m ago"
    if delta < day:
        return f"{int(delta / hour)}h ago"
    if delta < day * 14:
        return f"{int(delta / day)}d ago"
    return f"{int(delta / week)}w ago"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@snapshot_app.command("show")
def show(
    hash_or_prefix: str = typer.Argument(
        ...,
        help="Snapshot hash (full ``sha256:abc...``, short 8-char prefix, or ≥4-char prefix).",
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root. Defaults to walking up from cwd.",
    ),
    show_files: bool = typer.Option(
        False,
        "--files",
        help="Include the full file list (off by default — can be hundreds of entries).",
    ),
) -> None:
    """Display a snapshot's manifest in detail.

    Renders the header + metrics inline; the per-file table is gated
    behind ``--files`` so the default view stays readable for
    snapshots with hundreds of captured files.
    """
    project_root = _resolve_project_root(project)
    try:
        manifest = resolve_snapshot(project_root, hash_or_prefix)
    except SnapshotNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SnapshotStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    short = manifest.hash.removeprefix("sha256:")[:8]
    total_bytes = sum(f.size for f in manifest.files)

    body = (
        f"[bold]hash:[/bold]      {manifest.hash}\n"
        f"[bold]when:[/bold]      {manifest.created_at}\n"
        f"[bold]files:[/bold]     {len(manifest.files)} ({_format_bytes(total_bytes)})\n"
        f"[bold]agents:[/bold]    {manifest.agent_count}\n"
        f"[bold]workflows:[/bold] {manifest.workflow_count}\n"
    )
    if manifest.description:
        body += f"[bold]desc:[/bold]      {manifest.description}\n"
    body += (
        f"[bold]root:[/bold]      {manifest.project_root}\n"
        f"[bold]location:[/bold]  {project_root}/.movate/snapshots/{short}/"
    )

    console.print(
        Panel(
            body,
            title=f"snapshot [cyan]{short}[/cyan]",
            title_align="left",
            border_style="cyan",
        )
    )

    if show_files:
        console.print()
        file_table = Table(title="Captured files", title_style="bold")
        file_table.add_column("Path", style="cyan")
        file_table.add_column("Size", justify="right", style="dim")
        file_table.add_column("SHA-256", style="dim")
        for entry in manifest.files:
            file_table.add_row(
                entry.path,
                _format_bytes(entry.size),
                entry.sha256.removeprefix("sha256:")[:12] + "…",
            )
        console.print(file_table)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@snapshot_app.command("delete")
def delete(
    hash_or_prefix: str = typer.Argument(
        ...,
        help="Snapshot hash or short-hash prefix to remove.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Required to actually delete — the default behavior is to "
            "print what would be deleted and exit non-zero. Snapshots "
            "are immutable + part of the audit trail; deleting one "
            "should be a deliberate operator decision."
        ),
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root.",
    ),
) -> None:
    """Remove a snapshot from disk.

    [bold]Defaults to dry-run.[/bold] Run with ``--force`` to actually
    delete. Snapshots are immutable + part of the project audit trail;
    deleting one should be a deliberate decision, not a reflex.
    """
    project_root = _resolve_project_root(project)
    try:
        if not force:
            # Dry-run: resolve + print what would be deleted, exit 1.
            from movate.snapshot.store import resolve_snapshot as _resolve  # noqa: PLC0415

            manifest = _resolve(project_root, hash_or_prefix)
            short = manifest.hash.removeprefix("sha256:")[:8]
            console.print(
                f"[yellow]⚠ dry-run:[/yellow] would delete snapshot "
                f"[bold]{short}[/bold] ({len(manifest.files)} files, "
                f"created {manifest.created_at})"
            )
            console.print("[dim]Re-run with [bold]--force[/bold] to delete.[/dim]")
            raise typer.Exit(code=1)

        manifest = delete_snapshot(project_root, hash_or_prefix)
    except SnapshotNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SnapshotStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    short = manifest.hash.removeprefix("sha256:")[:8]
    console.print(
        f"[green]✓[/green] deleted snapshot [bold]{short}[/bold] ({len(manifest.files)} files)"
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


_KB_THRESHOLD = 1024
_MB_THRESHOLD = 1024 * 1024
_GB_THRESHOLD = 1024 * 1024 * 1024


def _format_bytes(n: int) -> str:
    """Human-friendly byte count (B / KB / MB / GB)."""
    if n < _KB_THRESHOLD:
        return f"{n} B"
    if n < _MB_THRESHOLD:
        return f"{n / _KB_THRESHOLD:.1f} KB"
    if n < _GB_THRESHOLD:
        return f"{n / _MB_THRESHOLD:.1f} MB"
    return f"{n / _GB_THRESHOLD:.2f} GB"
