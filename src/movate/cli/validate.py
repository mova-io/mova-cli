"""``movate validate <agent>`` — load + validate an agent directory."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from movate.core.loader import AgentLoadError, load_agent

console = Console()


def validate(
    path: Path = typer.Argument(..., help="Path to agent directory."),
) -> None:
    """Validate ``agent.yaml``, prompt template, and JSON schemas."""
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    spec = bundle.spec
    console.print(f"[green]✓[/green] {spec.name} [dim]v{spec.version}[/dim]")
    console.print(f"  api_version: {spec.api_version}")
    console.print(f"  provider:    {spec.model.provider}")
    console.print(f"  prompt:      {bundle.prompt_hash[:12]}…")
    if spec.model.fallback:
        fbs = ", ".join(f.provider for f in spec.model.fallback)
        console.print(f"  fallback:    {fbs}")
