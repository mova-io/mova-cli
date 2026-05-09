"""``movate validate <path>`` — load + validate an agent or a workflow.

Auto-detects: a path with ``workflow.yaml`` validates as a workflow (compile
+ ``validate_linear`` v0.3 phase gate); otherwise validates as an agent.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from movate.cli._workflow_path import is_workflow_path
from movate.core.loader import AgentLoadError, load_agent
from movate.core.workflow import (
    WorkflowCompileError,
    compile_workflow,
    load_workflow_spec,
    validate_linear,
)
from movate.core.workflow.spec import WorkflowSpecLoadError

console = Console()


def validate(
    path: Path = typer.Argument(..., help="Path to an agent or workflow directory."),
) -> None:
    """Validate ``agent.yaml`` (or ``workflow.yaml``) plus its references."""
    if is_workflow_path(path):
        _validate_workflow(path)
    else:
        _validate_agent(path)


def _validate_agent(path: Path) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    spec = bundle.spec
    console.print(f"[green]✓[/green] {spec.name} [dim]v{spec.version}[/dim] [dim](agent)[/dim]")
    console.print(f"  api_version: {spec.api_version}")
    console.print(f"  provider:    {spec.model.provider}")
    console.print(f"  prompt:      {bundle.prompt_hash[:12]}…")
    if spec.model.fallback:
        fbs = ", ".join(f.provider for f in spec.model.fallback)
        console.print(f"  fallback:    {fbs}")


def _validate_workflow(path: Path) -> None:
    try:
        spec, parent = load_workflow_spec(path)
    except WorkflowSpecLoadError as exc:
        console.print(f"[red]✗ workflow.yaml load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    try:
        graph = compile_workflow(spec, parent)
        validate_linear(graph)
    except WorkflowCompileError as exc:
        console.print(f"[red]✗ workflow validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    console.print(
        f"[green]✓[/green] {graph.name} [dim]v{graph.version}[/dim] [dim](workflow)[/dim]"
    )
    console.print(f"  api_version: {spec.api_version}")
    console.print(f"  entrypoint:  {graph.entrypoint}")
    console.print(f"  nodes:       {len(graph.nodes)}")
    console.print(f"  edges:       {len(graph.edges)}")
    chain = " → ".join(graph.topological_order())
    console.print(f"  topology:    {chain}")
