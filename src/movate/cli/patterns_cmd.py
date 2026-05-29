"""``mdk patterns`` — discover the governed agent-pattern templates (ADR 038).

The five functional patterns — chatbot, task-oriented, goal-oriented, monitor,
simulation — are GOVERNED realizations of ADR 038's pattern library: each bakes
in bounds (budgets, fan-out caps, max-iterations / turn caps), eval-gates, and
full tracing, composed from the EXISTING workflow primitives (ADR 017).

Scaffold one with ``mdk init <name> --pattern <pattern>``. This command surfaces
the catalog (name + one-liner + topology) for discoverability.

Only one subcommand today (``list``); kept as a subapp so future additions
(``show <name>``) slot in without breaking the surface.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from movate.templates import PATTERN_TEMPLATES, list_patterns

app = typer.Typer(
    name="patterns",
    help="Discover the governed agent-pattern templates (ADR 038) `mdk init --pattern` accepts.",
    no_args_is_help=True,
)

console = Console()


@app.command("list")
def list_cmd() -> None:
    """List the governed agent patterns + their topology + governance summary.

    Each row: the ``--pattern`` name, its one-line description, its topology
    (node graph), and whether it scaffolds a single agent or a workflow bundle.
    """
    table = Table(
        title=f"Governed agent patterns ({len(PATTERN_TEMPLATES)} total) — ADR 038",
        title_style="bold",
        header_style="bold cyan",
    )
    table.add_column("Pattern", style="cyan", no_wrap=True)
    table.add_column("Shape", style="dim", no_wrap=True)
    table.add_column("Topology", style="magenta", no_wrap=True)
    table.add_column("Description")

    for name in list_patterns():
        _dir, is_workflow, one_liner, topology = PATTERN_TEMPLATES[name]
        shape = "workflow" if is_workflow else "single agent"
        table.add_row(name, shape, topology, one_liner)

    console.print(table)
    console.print(
        "\n[dim]Each pattern is GOVERNED: bounds (budget / fan-out cap / "
        "max-iterations / turn cap) + eval-gate + full trace are baked in. "
        "See each bundle's [bold]GOVERNANCE.md[/bold] after scaffolding.[/dim]"
    )
    console.print(
        "[dim]Usage: [bold]mdk init <name> --pattern <pattern>[/bold] "
        "(scaffolds a runnable, governed project).[/dim]"
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
