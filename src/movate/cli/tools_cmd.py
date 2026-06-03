"""``mdk tools`` -- operator commands for the shared tool registry (ADR 052).

Four subcommands:

* ``mdk tools list`` -- list tool descriptors (filterable by scope, tag, query)
* ``mdk tools info <name>`` -- full detail for a tool descriptor
* ``mdk tools publish <tool.yaml>`` -- publish a skill as a tool descriptor
* ``mdk tools resolve <name>@<version>`` -- test resolution (scope/version)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


tools_app = typer.Typer(
    name="tools",
    help=(
        "Manage the shared tool registry (ADR 052). List, inspect, "
        "publish, and resolve tool descriptors."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# `mdk tools list`
# ---------------------------------------------------------------------------


@tools_app.command("list")
def list_tools(
    scope: str | None = typer.Option(
        None,
        "--scope",
        "-s",
        help="Filter by scope: movate, tenant, or project.",
    ),
    tag: str | None = typer.Option(
        None,
        "--tag",
        "-t",
        help="Filter by tag (exact match).",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        "-q",
        help="Freetext match against name and description.",
    ),
) -> None:
    """List tool descriptors in the registry.

    [bold]Examples:[/bold]

      [dim]# List all tools[/dim]
      $ mdk tools list

      [dim]# List tenant-scoped tools[/dim]
      $ mdk tools list --scope tenant

      [dim]# Filter by tag[/dim]
      $ mdk tools list --tag crm
    """
    from movate.storage import build_storage  # noqa: PLC0415

    async def _run() -> list[Any]:
        storage = build_storage()
        await storage.init()
        try:
            tags_filter = [tag] if tag else None
            descriptors = await storage.list_tool_descriptors(
                scope=scope,
                tenant_id="local",
                tags=tags_filter,
            )
            if query:
                q_lower = query.lower()
                descriptors = [
                    d
                    for d in descriptors
                    if q_lower in d.name.lower() or q_lower in d.description.lower()
                ]
            return descriptors
        finally:
            await storage.close()

    descriptors = asyncio.run(_run())

    if not descriptors:
        console.print("[dim]no tool descriptors found in the registry.[/dim]")
        console.print("[dim]hint: `mdk tools publish <tool.yaml>` to publish one.[/dim]")
        return

    table = Table(
        title="tool registry",
        show_header=True,
        header_style="bold",
    )
    table.add_column("name")
    table.add_column("version", style="dim")
    table.add_column("scope")
    table.add_column("backend")
    table.add_column("tags", style="dim")
    table.add_column("description", overflow="fold")

    for d in descriptors:
        tags_str = ", ".join(d.tags) if d.tags else ""
        desc = d.description.strip().splitlines()[0] if d.description.strip() else ""
        scope_val = d.scope if isinstance(d.scope, str) else d.scope.value
        backend_kind = d.backend.kind if hasattr(d.backend, "kind") else str(d.backend)
        table.add_row(
            d.name,
            d.version,
            scope_val,
            backend_kind,
            tags_str,
            desc,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# `mdk tools info <name>`
# ---------------------------------------------------------------------------


@tools_app.command("info")
def info(
    name: str = typer.Argument(
        ...,
        help="Tool name (dotted, e.g. 'jira.create-issue').",
    ),
) -> None:
    """Show full detail for a tool descriptor.

    [bold]Examples:[/bold]

      $ mdk tools info jira.create-issue
    """
    from movate.storage import build_storage  # noqa: PLC0415

    async def _run() -> Any:
        storage = build_storage()
        await storage.init()
        try:
            # Walk scope precedence.
            for scope_val in ("project", "tenant", "movate"):
                descriptor = await storage.get_tool_descriptor(
                    name=name,
                    version=None,
                    scope=scope_val,
                    tenant_id="local",
                )
                if descriptor is not None:
                    return descriptor
            return None
        finally:
            await storage.close()

    descriptor = asyncio.run(_run())

    if descriptor is None:
        err_console.print(f"[red]tool {name!r} not found in registry.[/red]")
        raise typer.Exit(code=2)

    scope_val = descriptor.scope if isinstance(descriptor.scope, str) else descriptor.scope.value
    backend_kind = (
        descriptor.backend.kind if hasattr(descriptor.backend, "kind") else str(descriptor.backend)
    )

    lines: list[str] = [
        f"[bold]name:[/bold]        {descriptor.name}",
        f"[bold]version:[/bold]     {descriptor.version}",
        f"[bold]scope:[/bold]       {scope_val}",
        f"[bold]backend:[/bold]     {backend_kind}",
        f"[bold]owner:[/bold]       {descriptor.owner or ''}",
        f"[bold]tags:[/bold]        {', '.join(descriptor.tags) if descriptor.tags else ''}",
        f"[bold]mutating:[/bold]    {descriptor.governance.mutating}",
        f"[bold]credentials:[/bold] {descriptor.credentials_ref or 'none'}",
    ]

    if descriptor.description:
        lines.append("")
        lines.append("[bold]description:[/bold]")
        for dline in descriptor.description.strip().splitlines():
            lines.append(f"  {dline}")

    console.print(
        Panel(
            "\n".join(lines),
            title=f"[bold]{descriptor.name}[/bold] [dim]v{descriptor.version}[/dim]",
            title_align="left",
            border_style="blue",
        )
    )

    console.print("[bold]Input schema:[/bold]")
    console.print(
        Syntax(
            json.dumps(descriptor.input_schema, indent=2),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    )

    console.print("[bold]Output schema:[/bold]")
    console.print(
        Syntax(
            json.dumps(descriptor.output_schema, indent=2),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    )


# ---------------------------------------------------------------------------
# `mdk tools publish <tool.yaml>`
# ---------------------------------------------------------------------------


@tools_app.command("publish")
def publish(
    path: Path = typer.Argument(
        ...,
        help="Path to a tool.yaml file describing the tool descriptor.",
    ),
) -> None:
    """Publish a tool descriptor to the registry.

    Reads a YAML file and publishes it as a tool descriptor in the
    tenant-scoped registry. The YAML must contain the ToolDescriptor
    fields (name, version, backend, etc.).

    [bold]Examples:[/bold]

      $ mdk tools publish tools/jira-create.yaml
    """
    from movate.core.tool_registry.models import ToolDescriptor  # noqa: PLC0415
    from movate.storage import build_storage  # noqa: PLC0415

    if not path.exists():
        err_console.print(f"[red]file not found:[/red] {path}")
        raise typer.Exit(code=2)

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        err_console.print(f"[red]invalid YAML:[/red] {exc}")
        raise typer.Exit(code=2) from None

    try:
        descriptor = ToolDescriptor.model_validate(raw)
    except Exception as exc:
        err_console.print(f"[red]validation failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    descriptor = descriptor.stamp_now()

    async def _run() -> None:
        storage = build_storage()
        await storage.init()
        try:
            await storage.save_tool_descriptor(descriptor)
        finally:
            await storage.close()

    asyncio.run(_run())
    console.print(
        f"[green]published[/green] [bold]{descriptor.name}[/bold] "
        f"v{descriptor.version} (scope={descriptor.scope})"
    )


# ---------------------------------------------------------------------------
# `mdk tools resolve <name>@<version>`
# ---------------------------------------------------------------------------


@tools_app.command("resolve")
def resolve(
    ref: str = typer.Argument(
        ...,
        help="Tool reference, e.g. 'jira.create-issue@^1.2.0' or bare name.",
    ),
) -> None:
    """Test tool resolution against the registry.

    Shows which scope and version resolves for the given reference.

    [bold]Examples:[/bold]

      $ mdk tools resolve jira.create-issue@^1.0.0
      $ mdk tools resolve servicenow.lookup-incident
    """
    from movate.core.tool_registry.resolver import (  # noqa: PLC0415
        ToolResolutionError,
        ToolResolver,
    )
    from movate.storage import build_storage  # noqa: PLC0415

    async def _run() -> Any:
        storage = build_storage()
        await storage.init()
        try:
            resolver = ToolResolver(store=storage, tenant_id="local")
            return await resolver.resolve(ref)
        finally:
            await storage.close()

    try:
        descriptor = asyncio.run(_run())
    except ToolResolutionError as exc:
        err_console.print(f"[red]resolution failed:[/red] {exc}")
        raise typer.Exit(code=2) from None

    scope_val = descriptor.scope if isinstance(descriptor.scope, str) else descriptor.scope.value
    console.print(
        f"[green]resolved[/green] [bold]{descriptor.name}[/bold] "
        f"v{descriptor.version} (scope={scope_val})"
    )
    console.print(
        Syntax(
            json.dumps(descriptor.model_dump(mode="json"), indent=2),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    )
