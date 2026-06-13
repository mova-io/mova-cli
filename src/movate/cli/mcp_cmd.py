"""``mdk mcp serve`` — the authoring MCP server (ADR 025 S3/D5, PR4).

Exposes the typed authoring action catalog (:mod:`movate.authoring.catalog`) as
MCP tools so a structured IDE/agent (Claude Code, Cursor, …) can drive the same
plan→preview→apply→verify spine the thin ``mdk authoring`` CLI does — one
catalog, three surfaces, no drift (D5). For each catalog action there is a
``plan_<action>`` (dry-run) and an ``apply_<action>`` (driven through the
checkpoint/verify/reversible driver) tool, plus catalog-wide ``validate`` and
``run`` tools. The tool schemas are generated from the catalog's self-describing
action arg schemas — never hand-maintained.

Boundaries (D8): the tools compose only catalog actions — no raw filesystem
writes, no shell, no ``az``, no credentials. This is a local control-plane
authoring tool (``cli`` ⊥ ``runtime``).

Dependency posture (CLAUDE.md §8): no new dependency. The server speaks
newline-delimited JSON-RPC 2.0 over stdio with the stdlib only — the same
hand-rolled decision the MCP *skill backend* made rather than pulling in the
heavy official ``mcp`` SDK. The server engine is imported lazily inside the
command body so importing ``movate.cli.main`` / running non-mcp commands never
pays for it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

from movate.cli._resolve import walk_up_for_project_root

console = Console()
err_console = Console(stderr=True)

mcp_app = typer.Typer(
    name="mcp",
    help=(
        "Run the authoring MCP server: expose the typed action catalog "
        "(ADR 025) as MCP tools (plan_*/apply_*/validate/run) so an IDE/agent "
        "drives the same plan→apply→verify spine as `mdk authoring`. Local "
        "control-plane tool; composes catalog actions only."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _resolve_project(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_dir():
            err_console.print(f"[red]✗[/red] --project path is not a directory: {explicit}")
            raise typer.Exit(code=2)
        return explicit.resolve()
    return walk_up_for_project_root() or Path.cwd().resolve()


@mcp_app.command("serve")
def serve(
    project: Path | None = typer.Option(
        None, "--project", "-p", help="Project root the catalog operates against."
    ),
    list_tools: bool = typer.Option(
        False,
        "--list-tools",
        help="Print the generated MCP tool manifest as JSON and exit (no server).",
    ),
) -> None:
    """Serve the authoring catalog over MCP (newline-delimited JSON-RPC on stdio).

    Runs until the client closes stdin. The catalog is exposed as a
    ``plan_<action>`` + ``apply_<action>`` tool per action, plus ``validate`` and
    ``run``; schemas come from the catalog. ``--list-tools`` prints the manifest
    and exits — useful for wiring up an MCP host config or a quick check.

    [bold]Examples:[/bold]

      [dim]# Inspect the tool manifest (no server)[/dim]
      [dim]$ mdk mcp serve --list-tools[/dim]

      [dim]# Run the stdio MCP server for an IDE/agent host[/dim]
      [dim]$ mdk mcp serve --project .[/dim]
    """
    # Lazy import: keep `movate.cli.main` import + non-mcp commands free of the
    # server engine (and its authoring/executor imports) until `mcp serve` runs.
    from movate.authoring.mcp_server import build_server, serve_stdio  # noqa: PLC0415

    root = _resolve_project(project)
    server = build_server(root)

    if list_tools:
        import json  # noqa: PLC0415

        console.print_json(json.dumps(server.tool_manifest()))
        return

    err_console.print(f"[dim]mdk authoring MCP server (catalog over stdio) — project {root}[/dim]")
    serve_stdio(server, sys.stdin, sys.stdout)


@mcp_app.command("inspect")
def inspect(
    entry: str = typer.Argument(
        ...,
        help=(
            "MCP server to probe: a stdio command (e.g. "
            "'npx -y @modelcontextprotocol/server-github') or an http(s):// URL."
        ),
    ),
    name: str = typer.Option(
        "server",
        "--name",
        "-n",
        help=(
            "Server handle used to preview the namespaced skill identifiers "
            "an agent would get (<name>.<tool>). Cosmetic for inspect."
        ),
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the raw tools/list descriptors as JSON and exit."
    ),
) -> None:
    """Probe an external MCP server and print its tools — without wiring anything (ADR 101 D4).

    The "what would I get if I declared this under ``mcp_servers:``?" check.
    Connects, calls ``tools/list``, and prints each tool with the namespaced
    skill identifier discovery would mint. Read-only: no files written, no
    skill registered. Auth uses ambient env (a token already exported); the
    ``credentials_ref`` injection path applies at run time, not here.

    [bold]Examples:[/bold]

      [dim]# Probe a stdio server[/dim]
      [dim]$ mdk mcp inspect 'npx -y @modelcontextprotocol/server-github' -n github[/dim]

      [dim]# Probe a remote server, machine-readable[/dim]
      [dim]$ mdk mcp inspect https://mcp.example.com/api --json[/dim]
    """
    import asyncio  # noqa: PLC0415

    from movate.core.mcp_discovery import _is_mutating, _sanitize_segment  # noqa: PLC0415
    from movate.core.skill_backend.base import SkillError  # noqa: PLC0415
    from movate.core.skill_backend.mcp import MCPSkillBackend  # noqa: PLC0415

    err_console.print(f"[dim]connecting to MCP server: {entry}[/dim]")
    backend = MCPSkillBackend()
    try:
        discovered = asyncio.run(backend.discover_tools(entry, name))
    except SkillError as exc:
        err_console.print(f"[red]✗ failed to connect to MCP server:[/red] {exc.message}")
        raise typer.Exit(code=2) from None
    finally:
        asyncio.run(backend.aclose())

    if as_json:
        import json  # noqa: PLC0415

        console.print_json(json.dumps(discovered))
        return

    if not discovered:
        err_console.print("[yellow]server reported zero tools.[/yellow]")
        return

    from rich.table import Table  # noqa: PLC0415

    table = Table(
        title=f"{len(discovered)} tool(s) on {entry}", show_header=True, header_style="bold"
    )
    table.add_column("tool (wire name)", style="bold")
    table.add_column("skill identifier", style="cyan")
    table.add_column("writes?", justify="center")
    table.add_column("description", overflow="fold")
    for t in discovered:
        wire = t.get("name", "?")
        seg = _sanitize_segment(wire) if isinstance(wire, str) else None
        ident = f"{name}-{seg}" if seg else "[red](unmintable)[/red]"
        writes = "[yellow]✎[/yellow]" if _is_mutating(t) else ""
        desc = t.get("description", "")
        desc = desc.strip().split("\n")[0][:120] if isinstance(desc, str) else ""
        table.add_row(str(wire), ident, writes, desc)
    console.print(table)
    console.print(
        f"[dim]Declare under mcp_servers: as "
        f"`- name: {name}` / `entry: {entry}` to register these.[/dim]"
    )


__all__ = ["mcp_app"]
