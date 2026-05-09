"""``movate show <agent>`` — print the resolved configuration for an agent."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate.core.loader import AgentLoadError, load_agent

console = Console()


def show(
    path: Path = typer.Argument(..., help="Path to agent directory."),
) -> None:
    """Print the resolved agent configuration."""
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
