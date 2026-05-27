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


__all__ = ["mcp_app"]
