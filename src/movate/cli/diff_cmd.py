"""``mdk diff <snap-a> <snap-b>`` — snapshot diff (Sprint N Day 4-5).

Companion to :mod:`movate.cli.snapshot_cmd`. Answers the most-common
operational question: *what changed between two snapshots?* — the
analog of ``git diff`` for the AI system's state graph.

Pure read-only: never modifies either snapshot, never touches disk
outside reading the two manifests.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate.snapshot import (
    SnapshotNotFoundError,
    SnapshotStoreError,
    diff_snapshots,
)
from movate.snapshot.diff import FileChange, SnapshotDiff
from movate.snapshot.store import resolve_snapshot

console = Console()
err_console = Console(stderr=True)


def _resolve_project_root(explicit: Path | None) -> Path:
    """Same walk-up resolution :mod:`snapshot_cmd` uses.

    Duplicated rather than imported to avoid a circular dependency
    between sibling CLI modules — both surfaces share the same
    convention but stay independently loadable.
    """
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


def diff(
    snap_a: str = typer.Argument(
        ...,
        help="'Before' snapshot — full hash or short-hash prefix (≥4 chars).",
    ),
    snap_b: str = typer.Argument(
        ...,
        help="'After' snapshot — full hash or short-hash prefix.",
    ),
    project: Path | None = typer.Option(
        None,
        "--project",
        "-p",
        help="Project root. Defaults to walking up from cwd for movate.yaml.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table — pipe-friendly for CI.",
    ),
    show_unchanged: bool = typer.Option(
        False,
        "--all",
        help=(
            "Include unchanged files in the table (off by default — "
            "diffs typically show only changes). Useful for audit "
            "trails that want the full picture."
        ),
    ),
) -> None:
    """Compare two snapshots and render what changed between them.

    Shows files added / removed / modified, plus agent + workflow
    count deltas. Answers \"what changed between green-bar and
    red-bar?\" — the most common operational failure-mode question.

    [bold]Examples:[/bold]

      [dim]# Compare two snapshots (typical use after `mdk snapshot list`)[/dim]
      $ mdk diff abc12345 def67890

      [dim]# Full UUID form also works[/dim]
      $ mdk diff sha256:abc12345... sha256:def67890...

      [dim]# CI gate — exit 1 if anything changed[/dim]
      $ mdk diff old-baseline current && echo "no drift"

      [dim]# JSON for piping to jq[/dim]
      $ mdk diff a b --json | jq '.files_modified[].path'
    """
    project_root = _resolve_project_root(project)

    try:
        before = resolve_snapshot(project_root, snap_a)
        after = resolve_snapshot(project_root, snap_b)
    except SnapshotNotFoundError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from None
    except SnapshotStoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    result = diff_snapshots(before, after)

    if json_output:
        console.print_json(json.dumps(_diff_as_json(result), default=str))
    else:
        _render_rich(result, show_unchanged=show_unchanged)

    # Non-zero exit when anything changed — lets CI use the command
    # as a drift gate: `mdk diff baseline current || alert`.
    if not result.is_identical:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _short(full_hash: str) -> str:
    """Extract the 8-char short hash for compact display."""
    return full_hash.removeprefix("sha256:")[:8]


def _render_rich(result: SnapshotDiff, *, show_unchanged: bool) -> None:
    """Render the diff as a Rich panel + tables.

    Three sections:
      1. Header — snapshot identifiers + descriptions
      2. Summary — counts (added/removed/modified, agent/workflow delta)
      3. File table — one row per change, kind-colored

    The ``show_unchanged`` flag is honored at the file-table level
    (no point rendering identical entries by default). The summary
    always shows the totals.
    """
    short_a = _short(result.before_hash)
    short_b = _short(result.after_hash)

    console.print()
    console.print(f"[bold]Diff:[/bold] [cyan]{short_a}[/cyan] → [cyan]{short_b}[/cyan]")
    if result.description_before or result.description_after:
        console.print(
            f"  [dim]{result.description_before or '—'}  →  {result.description_after or '—'}[/dim]"
        )
    console.print()

    if result.is_identical:
        console.print("[green]✓ no changes[/green]")
        return

    # Summary line
    summary_parts = []
    if result.files_added:
        summary_parts.append(f"[green]+{len(result.files_added)} added[/green]")
    if result.files_removed:
        summary_parts.append(f"[red]-{len(result.files_removed)} removed[/red]")
    if result.files_modified:
        summary_parts.append(f"[yellow]~{len(result.files_modified)} modified[/yellow]")
    console.print(f"[bold]Files:[/bold]   {'  '.join(summary_parts)}")

    if result.agent_count_delta or result.workflow_count_delta:
        meta_parts = []
        if result.agent_count_delta:
            sign = "+" if result.agent_count_delta > 0 else ""
            meta_parts.append(f"agents {sign}{result.agent_count_delta}")
        if result.workflow_count_delta:
            sign = "+" if result.workflow_count_delta > 0 else ""
            meta_parts.append(f"workflows {sign}{result.workflow_count_delta}")
        console.print(f"[bold]Counts:[/bold]  {' · '.join(meta_parts)}")

    console.print()

    # Per-file table
    table = Table(title="File changes", title_style="bold")
    table.add_column("Kind", no_wrap=True)
    table.add_column("Path", style="cyan")
    table.add_column("Size", justify="right", style="dim", no_wrap=True)

    changes = list(result.files_added) + list(result.files_removed) + list(result.files_modified)
    if not show_unchanged:
        # Already filtered; show_unchanged hook is for future symmetry
        # with `mdk audit` which surfaces all files for audit trails.
        pass
    for change in changes:
        table.add_row(*_render_row(change))
    console.print(table)


def _render_row(change: FileChange) -> tuple[str, str, str]:
    """One row of the file-changes table — colored by kind."""
    if change.kind == "added":
        kind = "[green]+ added[/green]"
        size = _bytes_str(change.after.size) if change.after else "—"
    elif change.kind == "removed":
        kind = "[red]- removed[/red]"
        size = _bytes_str(change.before.size) if change.before else "—"
    else:  # modified
        kind = "[yellow]~ modified[/yellow]"
        delta = change.size_delta or 0
        sign = "+" if delta > 0 else ""
        size = f"{sign}{delta} B"
    return (kind, change.path, size)


def _diff_as_json(result: SnapshotDiff) -> dict:
    """Serialise the diff to a JSON-friendly dict.

    Field names match the dataclass; FileChange entries flatten to
    {path, kind, size_delta}. Consumers (CI, dashboards) get a
    stable shape.
    """
    return {
        "before_hash": result.before_hash,
        "after_hash": result.after_hash,
        "description_before": result.description_before,
        "description_after": result.description_after,
        "total_changes": result.total_changes,
        "is_identical": result.is_identical,
        "agent_count_delta": result.agent_count_delta,
        "workflow_count_delta": result.workflow_count_delta,
        "files_added": [
            {"path": c.path, "size": c.after.size if c.after else None} for c in result.files_added
        ],
        "files_removed": [
            {"path": c.path, "size": c.before.size if c.before else None}
            for c in result.files_removed
        ],
        "files_modified": [
            {
                "path": c.path,
                "size_delta": c.size_delta,
                "before_sha256": c.before.sha256 if c.before else None,
                "after_sha256": c.after.sha256 if c.after else None,
            }
            for c in result.files_modified
        ],
    }


_KB_THRESHOLD = 1024
_MB_THRESHOLD = 1024 * 1024


def _bytes_str(n: int) -> str:
    """Compact byte rendering for the file-changes table."""
    if n < _KB_THRESHOLD:
        return f"{n} B"
    if n < _MB_THRESHOLD:
        return f"{n / _KB_THRESHOLD:.1f} KB"
    return f"{n / _MB_THRESHOLD:.1f} MB"
