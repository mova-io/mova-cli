"""Shared helper for Phase 0 command stubs.

Every command is wired into the Typer app at the right help panel so
``movate --help`` already shows the final shape of the CLI. Phase-by-phase
implementations replace these stubs in place.
"""

from __future__ import annotations

import typer
from rich.console import Console

console = Console(stderr=True)


def not_yet_implemented(name: str, phase: str) -> None:
    """Print a clear message and exit non-zero so CI/scripts notice."""
    console.print(
        f"[yellow]movate {name}[/yellow] is not implemented yet (landing in [bold]{phase}[/bold])."
    )
    raise typer.Exit(code=2)
