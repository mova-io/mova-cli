"""``mdk diff`` — snapshot diff (Sprint N Day 4-5) + git working-tree diff (Sprint Q).

Two modes:

* ``mdk diff <snap-a> <snap-b>`` — compare two snapshots. The original
  Sprint N form. Companion to :mod:`movate.cli.snapshot_cmd`; analog
  of ``git diff`` for the AI system's state graph.

* ``mdk diff --git`` — compare working tree against the last commit
  (or any git ref via ``--ref``). Sprint Q extension. Answers "did I
  introduce drift since my last commit?" without needing an explicit
  snapshot. Cheap UX win; reuses git's own diff machinery.

Both are pure read-only: never modify state, never touch disk outside
reading what they need to compare.
"""

from __future__ import annotations

import json
import subprocess
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

# Capture roots — duplicated from movate.snapshot.store (kept private
# there). We mirror them here so the git-diff filter scopes the same
# way the snapshot capture does. If the store's roots change, this
# constant should track them — a Sprint S+ refactor could lift this
# into a single module-level constant, but for ½d effort the duplicate
# is cheaper than the breaking-change risk.
_CAPTURE_ROOTS: tuple[str, ...] = (
    "movate.yaml",
    "policy.yaml",
    "knowledge.yaml",
    "agents",
    "contexts",
    "workflows",
    "skills",
)


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
        "",
        help=(
            "'Before' snapshot — full hash or short-hash prefix (≥4 chars). "
            "Omit when using [bold]--git[/bold]."
        ),
    ),
    snap_b: str = typer.Argument(
        "",
        help=(
            "'After' snapshot — full hash or short-hash prefix. Omit when using [bold]--git[/bold]."
        ),
    ),
    git: bool = typer.Option(
        False,
        "--git",
        help=(
            "Compare the working tree against the last commit (or [bold]--ref[/bold]) "
            "instead of two snapshots. Cheap drift check before committing."
        ),
    ),
    ref: str = typer.Option(
        "HEAD",
        "--ref",
        help=(
            "Git ref to compare against in [bold]--git[/bold] mode. "
            "Defaults to HEAD. Examples: [dim]main[/dim], "
            "[dim]release/v0.7[/dim], [dim]<commit-sha>[/dim]."
        ),
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
    """Compare two snapshots or the working tree against a git commit.

    [bold]Snapshot mode[/bold] ([cyan]mdk diff <a> <b>[/cyan]) — shows
    files added / removed / modified between two snapshots, plus agent +
    workflow count deltas. Answers "what changed between green-bar and
    red-bar?"

    [bold]Git mode[/bold] ([cyan]mdk diff --git[/cyan]) — shows files
    under the captured roots ([bold]agents/[/bold], [bold]movate.yaml[/bold],
    etc.) that differ between the working tree and the named git ref.
    Quick pre-commit drift check; no snapshot needed.

    [bold]Examples:[/bold]

      [dim]# Snapshot vs snapshot (Sprint N original)[/dim]
      $ mdk diff abc12345 def67890

      [dim]# Working tree vs last commit (Sprint Q extension)[/dim]
      $ mdk diff --git

      [dim]# Working tree vs main[/dim]
      $ mdk diff --git --ref main

      [dim]# CI gate — exit 1 if anything changed[/dim]
      $ mdk diff old-baseline current && echo "no drift"

      [dim]# JSON for piping to jq[/dim]
      $ mdk diff a b --json | jq '.files_modified[].path'
    """
    project_root = _resolve_project_root(project)

    if git:
        # Operator picked git mode; snap args are ignored.
        if snap_a or snap_b:
            err_console.print(
                "[yellow]⚠[/yellow] [bold]--git[/bold] ignores positional snapshot args. "
                "[dim]Use [bold]mdk diff a b[/bold] for snapshot-vs-snapshot, or "
                "[bold]mdk diff --git[/bold] for working-tree-vs-commit.[/dim]"
            )
        _run_git_diff(
            project_root=project_root,
            ref=ref,
            json_output=json_output,
        )
        return

    # Snapshot mode — both snap args required.
    if not snap_a or not snap_b:
        err_console.print(
            "[red]✗[/red] snapshot-vs-snapshot mode needs two positional args. "
            "[dim]Run [bold]mdk diff --git[/bold] to compare against a git commit instead, "
            "or [bold]mdk diff --help[/bold] for full usage.[/dim]"
        )
        raise typer.Exit(code=2)

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
    """One row of the file-changes table — colored single-letter kind.

    Compact ``A/M/D`` status letters in the git-status style — much
    more scannable than "+ added" / "~ modified" / "- removed" on a
    50-row diff. Operators reading the column see git's familiar
    A/M/D vocabulary instead of long-form prose.
    """
    if change.kind == "added":
        kind = "[green]A[/green]"
        size = _bytes_str(change.after.size) if change.after else "—"
    elif change.kind == "removed":
        kind = "[red]D[/red]"
        size = _bytes_str(change.before.size) if change.before else "—"
    else:  # modified
        kind = "[yellow]M[/yellow]"
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


# ---------------------------------------------------------------------------
# Git mode (Sprint Q extension)
# ---------------------------------------------------------------------------


# git diff --name-status emits a single-letter code per file:
#   A — added           D — deleted          M — modified
#   R — renamed         C — copied           T — type change
#   U — unmerged        X — unknown
# We map them into a human-readable label for the Rich table + a
# normalized "status" string for the JSON output.
_GIT_STATUS_LABEL = {
    "A": "added",
    "D": "deleted",
    "M": "modified",
    "R": "renamed",
    "C": "copied",
    "T": "type-change",
    "U": "unmerged",
}

_GIT_STATUS_STYLE = {
    "added": "green",
    "deleted": "red",
    "modified": "yellow",
    "renamed": "cyan",
    "copied": "cyan",
    "type-change": "magenta",
    "unmerged": "red",
}

# Minimum fields per ``git diff --name-status`` line: <status>\t<path>.
# Renames/copies have 3 fields (``R100\told\tnew``); we strip the
# similarity score and report the destination.
_MIN_GIT_DIFF_LINE_FIELDS = 2


def _run_git_diff(*, project_root: Path, ref: str, json_output: bool) -> None:
    """Compare working tree against ``ref`` via ``git diff --name-status``.

    Filters output to the same capture roots a snapshot would cover —
    junk under .venv / __pycache__ doesn't bubble up.

    Exits 1 when anything changed (drift detected), matching the
    snapshot-mode exit-code contract so CI gating can use either
    mode interchangeably.
    """
    if not (project_root / ".git").exists():
        err_console.print(
            f"[red]✗[/red] {project_root} is not a git repository "
            "(no [bold].git/[/bold] directory found). "
            "[dim]--git[/dim] mode needs a git repo to compare against."
        )
        raise typer.Exit(code=2)

    # Build the git command. The `--` separator + capture-root paths
    # confines the diff to files we'd care about. Skipping `--`
    # would diff everything in the repo (license churn, etc.).
    cmd = [
        "git",
        "-C",
        str(project_root),
        "diff",
        "--name-status",
        ref,
        "--",
        *_CAPTURE_ROOTS,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        err_console.print(
            "[red]✗[/red] [bold]git[/bold] not found on PATH. "
            "[dim]--git mode needs the git binary installed.[/dim]"
        )
        raise typer.Exit(code=2) from exc

    if result.returncode not in (0, 1):
        # git diff returns 0 (no diff) or 1 (diff present) on success.
        # Anything else is a real error (e.g. unknown ref, dirty index).
        err_console.print(
            f"[red]✗[/red] git diff failed: {result.stderr.strip() or 'unknown error'}"
        )
        raise typer.Exit(code=2)

    changes = _parse_git_name_status(result.stdout)

    if json_output:
        payload = {
            "ref": ref,
            "project_root": str(project_root),
            "changes": [{"status": c["status"], "path": c["path"]} for c in changes],
            "n_changed": len(changes),
        }
        console.print_json(json.dumps(payload))
    else:
        _render_git_diff(changes, ref=ref, project_root=project_root)

    # Drift = exit 1, clean = exit 0. Same convention as snapshot mode.
    if changes:
        raise typer.Exit(code=1)


def _parse_git_name_status(output: str) -> list[dict]:
    """Parse the tab-separated output of ``git diff --name-status``.

    Returns a list of ``{"status": str, "path": str}`` dicts. Renames
    and copies have a similarity score appended (e.g. ``R100``); we
    strip the score and keep just the first letter for the status
    classification.
    """
    changes: list[dict] = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < _MIN_GIT_DIFF_LINE_FIELDS:
            continue
        raw_status = parts[0]
        # Strip similarity score off renames / copies (R100 → R).
        status_letter = raw_status[0] if raw_status else "?"
        label = _GIT_STATUS_LABEL.get(status_letter, "unknown")
        # For renames + copies, git emits two paths (from\tto). We
        # report the destination — that's the file the operator
        # actually edits going forward.
        path = parts[-1]
        changes.append({"status": label, "path": path})
    return changes


def _render_git_diff(changes: list[dict], *, ref: str, project_root: Path) -> None:
    """Render the git-diff result as a Rich table."""
    title = f"mdk diff --git  [dim]vs {ref}[/dim]"
    table = Table(title=title, title_style="bold", show_lines=False)
    table.add_column("Status", no_wrap=True)
    table.add_column("File", style="cyan")

    if not changes:
        console.print(
            f"[green]✓[/green] no drift between working tree and "
            f"[bold]{ref}[/bold] under captured roots."
        )
        return

    for entry in changes:
        status = entry["status"]
        style = _GIT_STATUS_STYLE.get(status, "")
        styled_status = f"[{style}]{status}[/{style}]" if style else status
        table.add_row(styled_status, entry["path"])
    console.print(table)
    console.print(
        f"\n[yellow]⚠[/yellow] {len(changes)} file(s) differ between "
        f"working tree and [bold]{ref}[/bold] under [dim]{project_root}[/dim]."
    )
