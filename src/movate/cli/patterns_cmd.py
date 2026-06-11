"""``mdk patterns`` — discover the governed agent-pattern templates (ADR 038).

The patterns — chatbot, task-oriented, goal-oriented, monitor, simulation, and
the certification-grade workflow shapes that followed them — are GOVERNED
realizations of ADR 038's pattern library: each bakes in bounds (budgets,
fan-out caps, max-iterations / turn caps), eval-gates, and full tracing,
composed from the EXISTING workflow primitives (ADR 017).

Scaffold one with ``mdk init <name> --pattern <pattern>``. This command
surfaces the catalog for discoverability. Subcommands:

* ``list``   — table of every pattern (name / kind / topology / one-liner),
  with optional ``--json`` for scripts.
* ``search`` — case-insensitive substring filter over name + description +
  topology; same output shapes as ``list``.
* ``info``   — full description, topology, the exact ``mdk init`` snippet,
  and the template's file listing for one pattern.

The registry itself lives in :data:`movate.templates.PATTERN_TEMPLATES`;
this surface is strictly READ-ONLY over it.
"""

from __future__ import annotations

import difflib
import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.templates import PATTERN_TEMPLATES, get_pattern_path, list_patterns

# Table-column cap so the long registry descriptions don't blow out the
# layout. Mirrors the templates_cmd convention; ``info`` shows the full text.
_MAX_DESC_LEN = 70

# Max number of files surfaced by ``mdk patterns info <name>``. Workflow
# bundles ship nested agents/ trees (datasets, judges, schemas) that would
# otherwise dominate the screen; the truncation marker counts the remainder.
_MAX_FILE_ENTRIES = 50

app = typer.Typer(
    name="patterns",
    help="Discover the governed agent-pattern templates (ADR 038) `mdk init --pattern` accepts.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def _kind(is_workflow: bool) -> str:
    """Map the registry's is_workflow bool to the catalog's kind string."""
    return "workflow" if is_workflow else "agent"


def _init_command(name: str) -> str:
    """The exact scaffold snippet for one pattern — shown in info + JSON."""
    return f"mdk init <target-dir> --pattern {name}"


def _record(name: str) -> dict[str, str]:
    """One pattern as a JSON-friendly record.

    Shape: ``{name, kind, description, topology, init_command}`` — the
    catalog contract shared by ``list --json`` and ``search --json``
    (``info --json`` extends it with ``files``).
    """
    _dir, is_workflow, description, topology = PATTERN_TEMPLATES[name]
    return {
        "name": name,
        "kind": _kind(is_workflow),
        "description": description,
        "topology": topology,
        "init_command": _init_command(name),
    }


def _truncate(s: str, n: int = _MAX_DESC_LEN) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "…"


def _render_table(names: list[str], *, title: str) -> None:
    """Render the shared list/search Rich table for the given pattern names."""
    table = Table(title=title, title_style="bold", header_style="bold cyan")
    table.add_column("Pattern", style="cyan", no_wrap=True)
    table.add_column("Kind", style="dim", no_wrap=True)
    table.add_column("Topology", style="magenta", no_wrap=True)
    table.add_column("Description")

    for name in names:
        _dir, is_workflow, one_liner, topology = PATTERN_TEMPLATES[name]
        table.add_row(name, _kind(is_workflow), topology, _truncate(one_liner))

    console.print(table)
    console.print(
        "\n[dim]Each pattern is GOVERNED: bounds (budget / fan-out cap / "
        "max-iterations / turn cap) + eval-gate + full trace are baked in. "
        "See each bundle's [bold]GOVERNANCE.md[/bold] after scaffolding.[/dim]"
    )
    console.print(
        "[dim]Inspect one: [bold]mdk patterns info <pattern>[/bold]. "
        "Scaffold: [bold]mdk init <name> --pattern <pattern>[/bold] "
        "(a runnable, governed project).[/dim]"
    )


def _pattern_files(name: str) -> tuple[list[str], int]:
    """Relative file paths shipped by one pattern, capped + deterministic.

    Walks :func:`get_pattern_path` in sorted order, skipping hidden files and
    ``__pycache__`` artifacts from development checkouts. Returns the capped
    list plus the count of entries elided beyond :data:`_MAX_FILE_ENTRIES`.
    """
    root = get_pattern_path(name)
    files: list[str] = []
    truncated = 0
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if len(files) >= _MAX_FILE_ENTRIES:
            truncated += 1
            continue
        files.append(path.relative_to(root).as_posix())
    return files, truncated


@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit a JSON array of pattern records to stdout. Each record is "
            "``{name, kind, description, topology, init_command}``. Stdout-only "
            "— no Rich logs — so callers can pipe directly to ``jq``."
        ),
    ),
) -> None:
    """List the governed agent patterns + their topology + governance summary.

    Each row: the ``--pattern`` name, its kind (single agent vs. workflow
    bundle), its topology (node graph), and a truncated one-line description.
    Renders a Rich table by default; pass ``--json`` for machine output.
    """
    names = list_patterns()
    if json_output:
        typer.echo(json.dumps([_record(n) for n in names], indent=2, sort_keys=False))
        return
    _render_table(
        names,
        title=f"Governed agent patterns ({len(PATTERN_TEMPLATES)} total) — ADR 038",
    )


@app.command("search")
def search_cmd(
    term: str = typer.Argument(
        ..., help="Case-insensitive substring matched over name + description + topology."
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit the matching pattern records as a JSON array — same record "
            "shape as ``mdk patterns list --json``. An empty array means no "
            "match (exit code stays 0 so scripts can branch on the payload)."
        ),
    ),
) -> None:
    """Search the pattern catalog by name, description, or topology.

    Case-insensitive substring match — ``mdk patterns search human`` finds
    every pattern with a HUMAN gate; ``mdk patterns search temporal`` finds
    the durable-runtime shapes. Same output shapes as ``list``.
    """
    needle = term.lower()
    matches = [
        name
        for name in list_patterns()
        if needle in name.lower()
        or needle in PATTERN_TEMPLATES[name][2].lower()
        or needle in PATTERN_TEMPLATES[name][3].lower()
    ]
    if json_output:
        typer.echo(json.dumps([_record(n) for n in matches], indent=2, sort_keys=False))
        return
    if not matches:
        console.print(
            f"No patterns match [bold]{term}[/bold]. "
            f"Browse the full catalog: [bold]mdk patterns list[/bold]."
        )
        return
    _render_table(
        matches,
        title=f"Patterns matching “{term}” ({len(matches)} of {len(PATTERN_TEMPLATES)}) — ADR 038",
    )


@app.command("info")
def info_cmd(
    name: str = typer.Argument(..., help="Pattern name to inspect (see `mdk patterns list`)."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit one pattern record as JSON: the ``list --json`` shape plus "
            "``files`` (relative paths shipped by the template, capped at "
            f"{_MAX_FILE_ENTRIES} entries)."
        ),
    ),
) -> None:
    """Show full description, topology, scaffold snippet + files for one pattern.

    Fails with exit code 2 when the pattern name is unknown (suggesting the
    closest match) so scripts can distinguish usage errors from failures.
    Output is Rich by default; pass ``--json`` for a machine-readable record.
    """
    if name not in PATTERN_TEMPLATES:
        suggestions = difflib.get_close_matches(name, list_patterns(), n=3)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        err_console.print(
            f"[red]✗[/red] unknown pattern {name!r}.{hint} "
            f"See [bold]mdk patterns list[/bold] for the full catalog."
        )
        raise typer.Exit(code=2)

    record = _record(name)
    files, truncated = _pattern_files(name)

    if json_output:
        payload: dict[str, object] = {**record, "files": files}
        typer.echo(json.dumps(payload, indent=2, sort_keys=False))
        return

    body = (
        f"[bold]name:[/bold]         [cyan]{record['name']}[/cyan]\n"
        f"[bold]kind:[/bold]         {record['kind']}\n"
        f"[bold]topology:[/bold]     [magenta]{record['topology']}[/magenta]\n"
        f"[bold]description:[/bold]  {record['description']}\n"
        f"[bold]scaffold:[/bold]     [bold]{record['init_command']}[/bold]"
    )
    console.print(
        Panel(
            body,
            title=f"pattern [cyan]{name}[/cyan]",
            title_align="left",
            border_style="cyan",
        )
    )
    console.print(f"\n[bold]Files[/bold] ({len(files)} shown):")
    for rel in files:
        console.print(f"  {rel}")
    if truncated:
        console.print(f"  [dim]… {truncated} more entries elided[/dim]")
    console.print(
        f"\n[dim]Scaffold it: [bold]{record['init_command']}[/bold] "
        "(a runnable, governed project).[/dim]"
    )


def register_patterns_in_catalog() -> int:
    """Register the five patterns as ``source="movate"`` catalog entries.

    GETATTR-GUARDED (the catalog storage/registration surface is not on main
    yet): we probe for a registration API by import + ``getattr`` and degrade
    GRACEFULLY when it's absent — the patterns still work via ``--pattern``.

    Returns the number of patterns registered (0 when the catalog isn't
    available). Never raises on a missing catalog surface.
    """
    try:
        from movate import catalog as _catalog  # type: ignore[attr-defined]  # noqa: PLC0415
    except Exception:
        return 0

    register = getattr(_catalog, "register_entry", None) or getattr(_catalog, "register", None)
    if not callable(register):
        return 0

    registered = 0
    for name in list_patterns():
        _dir, is_workflow, one_liner, topology = PATTERN_TEMPLATES[name]
        try:
            register(
                name=name,
                source="movate",
                kind="workflow" if is_workflow else "agent",
                description=one_liner,
                topology=topology,
            )
            registered += 1
        except Exception:
            # A signature mismatch / partial catalog surface must never break
            # the CLI — patterns remain usable via --pattern.
            continue
    return registered
