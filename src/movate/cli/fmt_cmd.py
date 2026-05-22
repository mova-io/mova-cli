"""``mdk fmt`` — gofmt/prettier for movate config files (Sprint P).

Three modes:

* **write** (default) — rewrite each file in place.
* ``--check`` — exit non-zero if anything would change. CI-friendly.
* ``--diff`` — print a unified diff per changed file. No writes.

Paths:

* Bare: format every recognized file under the current project
  (``agents/**/agent.yaml``, ``*.yaml`` at root, ``prompts/**/*.md``,
  ``evals/**/*.jsonl``).
* Explicit: pass one or more paths to format only those files /
  directories.

Examples::

  $ mdk fmt                    # format the whole project
  $ mdk fmt --check            # CI: exit 1 if any file would change
  $ mdk fmt --diff             # preview changes without writing
  $ mdk fmt agents/triage/     # just this agent
  $ mdk fmt --check movate.yaml policy.yaml
"""

from __future__ import annotations

import difflib
import fnmatch
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax

from movate.fmt import (
    FormatError,
    FormatResult,
    detect_format,
    format_file,
)

console = Console()
err_console = Console(stderr=True)


# Directories we never recurse into — junk that has nothing to do with
# operator-facing config. Keeps `mdk fmt` from chewing through .venv.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".mdk",
        ".movate",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)


def _load_fmtignore(roots: list[Path]) -> list[str]:
    """Read patterns from a ``.fmtignore`` at any root, one glob per line.

    Empty lines + lines starting with ``#`` are skipped. Operators
    exempt vendored YAML / generated files by adding glob patterns
    matched against the relative path from the project root.

    Same convention as ``.gitignore`` semantically; uses fnmatch
    (shell glob) rather than git's pattern dialect to avoid pulling
    in a separate matcher dependency. Operators with complex
    ignore needs can run ``mdk fmt path1/ path2/`` to scope manually.
    """
    patterns: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        candidate = root / ".fmtignore"
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            patterns.append(stripped)
    return patterns


def _matches_ignore(path: Path, root: Path, patterns: list[str]) -> bool:
    """True when ``path`` matches any of the ignore globs.

    Patterns match against the path relative to ``root``, so a project's
    ``.fmtignore`` can use repo-rooted globs without worrying about
    the operator's cwd.
    """
    if not patterns:
        return False
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.as_posix()
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def _iter_formattable(roots: list[Path]) -> list[Path]:
    """Walk roots + return every recognized file we'd format.

    Sorted for stable output (matters for the ``--check`` use case where
    operators read the list to fix files manually). Honors a
    ``.fmtignore`` file at any root.
    """
    ignore_patterns = _load_fmtignore(roots)
    found: list[Path] = []
    for root in roots:
        if root.is_file():
            if detect_format(root) is not None:
                found.append(root)
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            # Skip anything under a junk directory.
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if _matches_ignore(path, root, ignore_patterns):
                continue
            if detect_format(path) is not None:
                found.append(path)
    return sorted(set(found))


def _render_diff(result: FormatResult) -> None:
    """Print a unified diff for one changed file (with Rich syntax)."""
    diff = "".join(
        difflib.unified_diff(
            result.before.splitlines(keepends=True),
            result.after.splitlines(keepends=True),
            fromfile=f"{result.path} (current)",
            tofile=f"{result.path} (formatted)",
            n=3,
        )
    )
    if not diff:
        return
    console.print(Syntax(diff, "diff", theme="ansi_dark", line_numbers=False))


def fmt(  # noqa: PLR0912 — branch count is inherent to mode dispatch + summary
    paths: list[Path] = typer.Argument(
        None,
        help=(
            "Files or directories to format. "
            "Omit to format the whole project from the current working directory."
        ),
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help=(
            "CI mode: exit 1 if any file would change, don't write. "
            "Mutually exclusive with [bold]--diff[/bold]."
        ),
    ),
    diff: bool = typer.Option(
        False,
        "--diff",
        help=(
            "Print unified diff per changed file without writing. "
            "Mutually exclusive with [bold]--check[/bold]."
        ),
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Only print the summary line; suppress per-file output.",
    ),
) -> None:
    """Normalize the style of YAML / prompt / JSONL files.

    Default behavior is to rewrite files in place. Use [bold]--check[/bold]
    in CI to fail the build on style drift, or [bold]--diff[/bold] to
    preview changes locally before applying them.

    [bold]Examples:[/bold]

      [dim]$ mdk fmt                          # format the whole project[/dim]
      [dim]$ mdk fmt --check                  # CI gate[/dim]
      [dim]$ mdk fmt --diff                   # preview only[/dim]
      [dim]$ mdk fmt agents/triage/           # just one agent[/dim]
    """
    if check and diff:
        err_console.print(
            "[red]✗[/red] --check and --diff are mutually exclusive. [dim]Pick one mode.[/dim]"
        )
        raise typer.Exit(code=2)

    roots: list[Path] = [Path.cwd()] if not paths else [Path(p).resolve() for p in paths]

    targets = _iter_formattable(roots)
    if not targets:
        console.print(
            "[yellow]⚠[/yellow] no formattable files found. "
            "[dim]mdk fmt looks for agent.yaml, movate.yaml, policy.yaml, "
            "prompt.md, and *.jsonl files.[/dim]"
        )
        return

    write_mode = not (check or diff)

    changed: list[FormatResult] = []
    errored: list[tuple[Path, str]] = []

    for target in targets:
        try:
            result = format_file(target, write=write_mode)
        except FormatError as exc:
            errored.append((target, str(exc)))
            continue
        if not result.changed:
            continue
        changed.append(result)
        if diff:
            console.print(f"[cyan]{target}[/cyan]")
            _render_diff(result)
        elif not quiet:
            verb = "would format" if check else "formatted"
            console.print(f"[green]✓[/green] {verb} [cyan]{target}[/cyan]")

    # Errors first — operators need to know if anything failed to parse.
    if errored:
        for path, message in errored:
            err_console.print(f"[red]✗[/red] {path}: {message}")

    # Summary.
    total = len(targets)
    n_changed = len(changed)
    n_errored = len(errored)
    n_clean = total - n_changed - n_errored

    if check:
        if n_changed > 0:
            console.print(
                f"\n[red]✗[/red] {n_changed} of {total} file(s) would be reformatted. "
                f"[dim]Run [bold]mdk fmt[/bold] to apply.[/dim]"
            )
            raise typer.Exit(code=1)
        if n_errored > 0:
            raise typer.Exit(code=2)
        console.print(f"[green]✓[/green] {n_clean} file(s) already formatted")
        return

    if n_errored > 0:
        console.print(f"\n[red]✗[/red] {n_errored} file(s) failed to parse")
        raise typer.Exit(code=2)

    if n_changed == 0:
        console.print(f"[green]✓[/green] {n_clean} file(s) already formatted")
        return

    suffix = "" if diff else f" — wrote {n_changed} file(s)"
    console.print(
        f"\n[green]✓[/green] {n_changed} reformatted, {n_clean} clean (of {total} total){suffix}"
    )
