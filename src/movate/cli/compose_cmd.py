"""``mdk compose <name>`` — declarative multi-agent assembly (Sprint U MVP).

Scaffolds a new workflow.yaml under ``workflows/<name>/`` from a list
of agent names. The resulting workflow is sequential (each agent's
output feeds the next) by default. Operators edit the generated YAML
to add conditional edges, parallel forks, HITL gates as the engine
matures (Sprint U+).

Usage::

  mdk compose customer-flow --agents triage,summarise,respond
  mdk compose returns --agents validate,fraud-check,approve --runtime langgraph
  mdk compose flow --agents a,b,c --output workflows/custom/flow.yaml

[bold]Sprint U scope:[/bold] this is the OPERATOR-FACING entry point
for multi-agent composition. The underlying engine work (conditional
routing, parallel, HITL, checkpointing) is staged across Sprint U+.
The scaffolded workflow uses the linear-only IR that's been in
movate.core.workflow since Sprint M.

[bold]LangGraph swap-in:[/bold] passing ``--runtime langgraph`` emits
a workflow.yaml with ``runtime: langgraph`` in the metadata block.
The actual LangGraph compiler ships as a scaffold in
:mod:`movate.core.workflow.compilers.langgraph` — operators can
generate code, but the conditional-routing engine work continues
behind the same workflow.yaml contract.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel

console = Console()
err_console = Console(stderr=True)


def _slug_id(name: str) -> str:
    """workflow id from --name (alphanumeric + hyphen/underscore)."""
    import re  # noqa: PLC0415

    cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return cleaned or "workflow"


def _scaffold_workflow_yaml(
    *,
    workflow_name: str,
    agent_names: list[str],
    runtime: str,
    description: str,
) -> dict:
    """Build the workflow.yaml dict for ``mdk compose``.

    Each agent becomes a node; consecutive agents are wired with a
    sequential edge. Operators can edit the result to insert
    conditional / parallel constructs once the engine supports them.
    """
    nodes = []
    for agent_name in agent_names:
        nodes.append(
            {
                "id": _slug_id(agent_name),
                "type": "agent",
                # Conventional location for an agent dir; operators rewire
                # if their layout is different.
                "ref": f"../../agents/{_slug_id(agent_name)}",
            }
        )
    edges = []
    for src, dst in pairwise(nodes):
        edges.append({"from": src["id"], "to": dst["id"]})

    spec: dict = {
        "api_version": "movate/v1",
        "kind": "Workflow",
        "name": _slug_id(workflow_name),
        "version": "0.1.0",
    }
    if description:
        spec["description"] = description
    spec["nodes"] = nodes
    spec["edges"] = edges
    if runtime == "langgraph":
        # Reserved metadata block — picked up by the compiler scaffold.
        # Sprint U+ wires this through `mdk show workflow` + the
        # runtime selector that currently only knows about the native
        # IR-driven runner.
        spec["runtime"] = "langgraph"
    return spec


def compose(
    name: str = typer.Argument(
        ...,
        help="Workflow name. Used as directory + spec.name.",
        metavar="NAME",
    ),
    agents: str = typer.Option(
        "",
        "--agents",
        help=(
            "Comma-separated agent names to wire in sequence "
            "(e.g. [dim]--agents triage,summarise,respond[/dim])."
        ),
    ),
    runtime: str = typer.Option(
        "native",
        "--runtime",
        help=(
            "Workflow runtime. [bold]native[/bold] (default) uses the "
            "movate workflow IR. [bold]langgraph[/bold] marks the workflow "
            "for the LangGraph compiler (scaffold today; full engine in Sprint U+)."
        ),
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="One-line workflow description recorded in spec.description.",
    ),
    output: str = typer.Option(
        "",
        "--output",
        "-o",
        help=(
            "Where to write the workflow.yaml. "
            "Default: [bold]workflows/<name>/workflow.yaml[/bold] under project root."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite OUTPUT if it already exists.",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        hidden=True,
    ),
) -> None:
    """Scaffold a multi-agent workflow.yaml from a list of agents.

    Each [bold]--agents[/bold] entry becomes one node; consecutive
    entries are wired with sequential edges. The generated file uses
    the linear workflow IR (Sprint M baseline); conditional routing
    / parallel / HITL land as the engine matures (Sprint U+).

    [bold]Examples:[/bold]

      [dim]$ mdk compose customer-flow --agents triage,summarise,respond[/dim]
      [dim]$ mdk compose returns --agents validate,fraud-check,approve \\[/dim]
      [dim]    --runtime langgraph[/dim]
      [dim]$ mdk compose flow --agents a,b --output workflows/x/flow.yaml[/dim]
    """
    if runtime not in ("native", "langgraph"):
        err_console.print(
            f"[red]✗[/red] --runtime must be 'native' or 'langgraph'; got {runtime!r}"
        )
        raise typer.Exit(code=2)

    agent_list = [a.strip() for a in agents.split(",") if a.strip()] if agents else []
    if not agent_list:
        err_console.print(
            "[red]✗[/red] --agents required. "
            "[dim]Example: [bold]--agents triage,summarise,respond[/bold].[/dim]"
        )
        raise typer.Exit(code=2)

    root = Path(project_root).resolve()
    wf_name = _slug_id(name)
    target = Path(output).resolve() if output else root / "workflows" / wf_name / "workflow.yaml"

    if target.exists() and not force:
        err_console.print(
            f"[red]✗[/red] {target} already exists (pass [bold]--force[/bold] to overwrite)"
        )
        raise typer.Exit(code=2)

    spec = _scaffold_workflow_yaml(
        workflow_name=name,
        agent_names=agent_list,
        runtime=runtime,
        description=description,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))

    body = (
        f"[bold]Workflow:[/bold] [cyan]{wf_name}[/cyan]\n"
        f"[bold]Path:[/bold]     [cyan]{target}[/cyan]\n"
        f"[bold]Runtime:[/bold]  [cyan]{runtime}[/cyan]\n"
        f"[bold]Nodes:[/bold]    {len(spec['nodes'])} "
        f"({' → '.join(n['id'] for n in spec['nodes'])})\n"
        f"[bold]Edges:[/bold]    {len(spec['edges'])}\n\n"
        "[bold]Next:[/bold]\n"
        f"  • [cyan]mdk validate {target.parent}[/cyan]\n"
        f"  • [cyan]mdk run {target.parent} '{{...}}'[/cyan]"
    )
    if runtime == "langgraph":
        body += (
            "\n  • [dim]LangGraph compiler is a scaffold today;\n"
            "    [cyan]mdk run[/cyan] still uses the native IR until "
            "Sprint U+ wires the compiler.[/dim]"
        )
    console.print(
        Panel(
            body,
            title="[green]✓[/green] Workflow scaffolded",
            title_align="left",
            border_style="green",
        )
    )
