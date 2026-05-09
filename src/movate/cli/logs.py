"""``movate logs`` — tail run / job output."""

from __future__ import annotations

import typer

from movate.cli._stub import not_yet_implemented


def logs(
    run_id: str = typer.Argument(..., help="Run or job id."),
) -> None:
    """Tail logs for a run."""
    _ = run_id
    not_yet_implemented("logs", "v0.4")
