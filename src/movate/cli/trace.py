"""``movate trace`` — replay a workflow trace."""

from __future__ import annotations

import typer

from movate.cli._stub import not_yet_implemented

trace_app = typer.Typer(
    name="trace",
    help="Inspect and replay traces.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@trace_app.command("replay")
def replay(
    run_id: str = typer.Argument(..., help="Run id to replay."),
) -> None:
    """Replay a workflow execution from local storage."""
    _ = run_id
    not_yet_implemented("trace replay", "v0.4")
