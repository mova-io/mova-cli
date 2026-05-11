"""``movate show <path>`` — print the resolved configuration for an agent or workflow."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from movate.cli._completion import complete_agent_path
from movate.cli._workflow_path import is_workflow_path
from movate.core.loader import AgentLoadError, load_agent
from movate.core.workflow import (
    WorkflowCompileError,
    WorkflowGraph,
    compile_workflow,
    load_workflow_spec,
)
from movate.core.workflow.spec import WorkflowSpecLoadError

console = Console()


def show(
    path: Path = typer.Argument(
        ...,
        help="Path to an agent or workflow directory.",
        shell_complete=complete_agent_path,
    ),
) -> None:
    """Show the resolved spec for an agent or workflow."""
    if is_workflow_path(path):
        _show_workflow(path)
    else:
        _show_agent(path)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def _show_agent(path: Path) -> None:
    try:
        bundle = load_agent(path)
    except AgentLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    spec = bundle.spec
    table = Table(title=f"{spec.name} v{spec.version}", show_header=False)
    table.add_column("field", style="dim")
    table.add_column("value")

    table.add_row("api_version", spec.api_version)
    table.add_row("kind", spec.kind)
    table.add_row("name", spec.name)
    table.add_row("version", spec.version)
    table.add_row("description", spec.description or "[dim]—[/dim]")
    table.add_row("owner", spec.owner or "[dim]—[/dim]")
    table.add_row("", "")
    table.add_row("model.provider", spec.model.provider)
    if spec.model.params:
        table.add_row("model.params", json.dumps(spec.model.params))
    if spec.model.fallback:
        fb = ", ".join(f.provider for f in spec.model.fallback)
        table.add_row("model.fallback", fb)
    table.add_row("", "")
    table.add_row("prompt", str(spec.prompt))
    table.add_row("prompt_hash", bundle.prompt_hash[:16] + "…")
    table.add_row("input.schema", str(spec.schemas.input))
    in_required = bundle.input_schema.get("required", [])
    table.add_row("  required", ", ".join(in_required) if in_required else "[dim]—[/dim]")
    table.add_row("output.schema", str(spec.schemas.output))
    out_required = bundle.output_schema.get("required", [])
    table.add_row("  required", ", ".join(out_required) if out_required else "[dim]—[/dim]")
    table.add_row("", "")
    if spec.evals.dataset:
        ds_path = (bundle.agent_dir / spec.evals.dataset).resolve()
        if ds_path.exists():
            raw = ds_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:12]
            count = sum(1 for line in raw.decode().splitlines() if line.strip())
            table.add_row("evals.dataset", f"{spec.evals.dataset} ({count} cases, sha={digest}…)")
        else:
            table.add_row("evals.dataset", f"{spec.evals.dataset} [red](missing)[/red]")
    table.add_row("timeouts", f"call={spec.timeouts.call_ms}ms total={spec.timeouts.total_ms}ms")
    table.add_row("budget", f"${spec.budget.max_cost_usd_per_run:.4f}/run")
    if spec.tags:
        table.add_row("tags", ", ".join(spec.tags))

    console.print(table)
    console.print(f"[dim]agent_dir: {bundle.agent_dir}[/dim]")


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


def _show_workflow(path: Path) -> None:
    try:
        spec, parent = load_workflow_spec(path)
    except WorkflowSpecLoadError as exc:
        console.print(f"[red]✗ load failed:[/red] {exc}")
        raise typer.Exit(code=2) from None
    try:
        graph = compile_workflow(spec, parent)
    except WorkflowCompileError as exc:
        console.print(f"[red]✗ compile failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    # Header table
    header = Table(title=f"{graph.name} v{graph.version} [dim](workflow)[/dim]", show_header=False)
    header.add_column("field", style="dim")
    header.add_column("value")
    header.add_row("api_version", spec.api_version)
    header.add_row("kind", "Workflow")
    header.add_row("description", graph.description or "[dim]—[/dim]")
    header.add_row("entrypoint", graph.entrypoint)
    header.add_row("nodes", str(len(graph.nodes)))
    header.add_row("edges", str(len(graph.edges)))
    state_required = graph.state_schema.get("required", [])
    header.add_row(
        "state.required",
        ", ".join(state_required) if state_required else "[dim]—[/dim]",
    )
    console.print(header)

    # Nodes
    nodes = Table(title="Nodes", show_header=True, header_style="bold")
    nodes.add_column("#", style="dim", width=3)
    nodes.add_column("id")
    nodes.add_column("type")
    nodes.add_column("ref", overflow="fold")
    for i, node_id in enumerate(graph.topological_order(), start=1):
        node = graph.nodes[node_id]
        nodes.add_row(str(i), node_id, node.type.value, node.ref)
    console.print(nodes)

    # Topology — ASCII chain (small, scannable)
    chain = " → ".join(graph.topological_order())
    console.print(f"\n[dim]topology:[/dim] {chain}\n")

    # Mermaid block — copy-paste into a GitHub PR description
    mermaid = render_mermaid(graph)
    console.print("[dim]Mermaid (paste into a PR):[/dim]")
    console.print(Syntax(mermaid, "mermaid", theme="ansi_dark", line_numbers=False))
    console.print(f"[dim]workflow_dir: {graph.workflow_dir}[/dim]")


def render_mermaid(graph: WorkflowGraph) -> str:
    """Render a Mermaid ``flowchart LR`` for the workflow's topology."""
    lines = ["flowchart LR"]
    for node_id in graph.topological_order():
        node = graph.nodes[node_id]
        # Use the node id as both the mermaid id and the visible label.
        lines.append(f"    {node_id}[{node_id}<br/><sub>{node.type.value}</sub>]")
    for edge in graph.edges:
        lines.append(f"    {edge.from_id} --> {edge.to_id}")
    return "\n".join(lines)
