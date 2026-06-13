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
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

from movate.cli._resolve import walk_up_for_project_root

if TYPE_CHECKING:
    from movate.mcp_catalog.models import CatalogEntry

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

    async def _probe() -> list[dict[str, Any]]:
        # discover + aclose MUST share one event loop: the stdio subprocess is
        # bound to the loop that spawned it, so tearing it down in a second
        # asyncio.run() raises "Event loop is closed".
        try:
            return await backend.discover_tools(entry, name)
        finally:
            await backend.aclose()

    try:
        discovered = asyncio.run(_probe())
    except SkillError as exc:
        err_console.print(f"[red]✗ failed to connect to MCP server:[/red] {exc.message}")
        raise typer.Exit(code=2) from None

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


# ---------------------------------------------------------------------------
# Catalog commands (ADR 103/104): list / search / add
# ---------------------------------------------------------------------------

_TRUST_STYLE = {"curated": "green", "official": "cyan", "community": "yellow"}


def _render_entries(entries: list[CatalogEntry], *, title: str) -> None:
    from rich.table import Table  # noqa: PLC0415

    table = Table(title=title, show_header=True, header_style="bold")
    table.add_column("name", style="bold")
    table.add_column("trust")
    table.add_column("transport")
    table.add_column("pinned", justify="center")
    table.add_column("description", overflow="fold")
    for e in entries:
        trust = e.trust.value if hasattr(e.trust, "value") else str(e.trust)
        table.add_row(
            e.name,
            f"[{_TRUST_STYLE.get(trust, 'white')}]{trust}[/]",
            e.transport,
            "•" if e.pinned else "[yellow]![/yellow]",
            (e.title + " — " + e.description).strip(" —"),
        )
    console.print(table)


@mcp_app.command("list")
def list_catalog(
    source: str | None = typer.Option(
        None,
        "--source",
        "-s",
        help="Source to list (default: bundled). Use 'all' for every source.",
    ),
) -> None:
    """List MCP servers in the catalog (ADR 103 D3).

    Default shows the curated, offline bundled catalog. ``--source official``
    lists the canonical registry; ``--source all`` includes community
    directories (each row shows its trust tier).
    """
    import asyncio  # noqa: PLC0415

    from movate.mcp_catalog.sources import UnknownSourceError, resolve_sources  # noqa: PLC0415

    # `list` with no --source shows just the bundled catalog (browsing the whole
    # live registry unfiltered is rarely useful); search is the live entry point.
    try:
        sources = resolve_sources(source or "bundled")
    except UnknownSourceError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    entries: list[CatalogEntry] = []
    for src in sources:
        entries.extend(asyncio.run(src.search("", limit=100)))
    if not entries:
        err_console.print("[yellow]no catalog entries.[/yellow]")
        return
    _render_entries(entries, title=f"MCP catalog ({source or 'bundled'})")
    console.print("[dim]Add one with: mdk mcp add <name> --agent <dir> | --project[/dim]")


@mcp_app.command("search")
def search_catalog(
    query: str = typer.Argument(
        ..., help="Substring to match against name/title/description/tags."
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        "-s",
        help=(
            "Source(s) to search. Default: bundled + official. A specific name "
            "(e.g. 'mcp.so') opts into a community source; 'all' searches every source."
        ),
    ),
    limit: int = typer.Option(25, "--limit", "-n", help="Max results per source."),
) -> None:
    """Search the catalog + registries for an MCP server (ADR 103 D3 / 104).

    Default searches the bundled catalog and the official registry. Community
    directories are opt-in (``--source mcp.so`` / ``--source glama`` / ``--source all``)
    and every row shows its trust tier so you see where an entry came from.
    """
    import asyncio  # noqa: PLC0415

    from movate.mcp_catalog.sources import UnknownSourceError, resolve_sources  # noqa: PLC0415

    try:
        sources = resolve_sources(source)
    except UnknownSourceError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    seen: set[str] = set()
    entries: list[CatalogEntry] = []
    for src in sources:
        for e in asyncio.run(src.search(query, limit=limit)):
            key = f"{e.source}:{e.name}"
            if key not in seen:
                seen.add(key)
                entries.append(e)
    if not entries:
        err_console.print(f"[yellow]no matches for {query!r}.[/yellow]")
        return
    _render_entries(entries, title=f"MCP search: {query!r}")


@mcp_app.command("add")
def add_catalog(  # noqa: PLR0912 — orchestrator: resolve → validate → probe → write
    name: str = typer.Argument(..., help="Catalog entry name to add (see `mdk mcp search`)."),
    agent: Path | None = typer.Option(
        None, "--agent", "-a", help="Agent directory whose agent.yaml gets the mcp_servers entry."
    ),
    project: bool = typer.Option(
        False, "--project", help="Add to the project's project.yaml (shared by every agent)."
    ),
    tools: str | None = typer.Option(
        None, "--tools", help="Comma-separated tool allowlist → include_tools (verbatim MCP names)."
    ),
    source: str | None = typer.Option(
        None, "--source", "-s", help="Source to resolve from (default: bundled + official)."
    ),
    no_inspect: bool = typer.Option(
        False, "--no-inspect", help="Skip the live tools/list reachability probe."
    ),
) -> None:
    """Resolve a catalog entry and write it into an agent/project as mcp_servers (ADR 103 D2).

    Resolves ``name`` from the catalog, optionally probes the server (reuses
    `mdk mcp inspect`), then writes a pinned ADR 101 ``mcp_servers:`` entry —
    idempotent by name. Writes a *declaration only*: it never installs or runs a
    server, and never stores a secret (it tells you which env var to set).

    [bold]Examples:[/bold]

      [dim]$ mdk mcp add github --agent agents/support-bot[/dim]
      [dim]$ mdk mcp add github --project --tools search_repositories,get_file_contents[/dim]
    """
    import asyncio  # noqa: PLC0415

    from movate.cli._resolve import walk_up_for_project_root  # noqa: PLC0415
    from movate.mcp_catalog.sources import UnknownSourceError, resolve_sources  # noqa: PLC0415

    # Exactly one destination.
    if agent is None and not project:
        err_console.print("[red]✗[/red] specify a destination: --agent <dir> or --project")
        raise typer.Exit(code=2)
    if agent is not None and project:
        err_console.print("[red]✗[/red] --agent and --project are mutually exclusive")
        raise typer.Exit(code=2)

    # Resolve the entry from the chosen source(s).
    try:
        sources = resolve_sources(source)
    except UnknownSourceError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=2) from None

    entry: CatalogEntry | None = None
    for src in sources:
        entry = asyncio.run(src.get(name))
        if entry is not None:
            break
    if entry is None:
        err_console.print(
            f"[red]✗[/red] no catalog entry {name!r} in source(s) "
            f"{source or 'bundled, official'}. Try `mdk mcp search {name}`."
        )
        raise typer.Exit(code=2)

    # Resolve destination file.
    if agent is not None:
        target = (agent / "agent.yaml") if agent.is_dir() else agent
        if not target.exists():
            err_console.print(f"[red]✗[/red] no agent.yaml at {target}")
            raise typer.Exit(code=2)
    else:
        root = walk_up_for_project_root()
        if root is None:
            err_console.print(
                "[red]✗[/red] no project root found (project.yaml). Run inside a project."
            )
            raise typer.Exit(code=2)
        target = root / "project.yaml"
        if not target.exists():
            err_console.print(f"[red]✗[/red] no project.yaml at {target}")
            raise typer.Exit(code=2)

    # Build the mcp_servers entry (the ADR 101 shape).
    server_entry: dict[str, Any] = {"name": entry.name, "entry": entry.entry}
    if entry.credentials:
        server_entry["credentials_ref"] = entry.credentials
    if tools:
        server_entry["include_tools"] = [t.strip() for t in tools.split(",") if t.strip()]

    # Validate it parses as an MCPServerRef before touching the file.
    from movate.core.models import MCPServerRef  # noqa: PLC0415

    try:
        MCPServerRef.model_validate(server_entry)
    except Exception as exc:
        err_console.print(f"[red]✗[/red] resolved entry is not a valid mcp_servers block: {exc}")
        raise typer.Exit(code=2) from None

    # Trust + pin warnings (ADR 104 D4/D5).
    trust = entry.trust.value if hasattr(entry.trust, "value") else str(entry.trust)
    if trust == "community":
        err_console.print(
            f"[yellow]⚠ {entry.name!r} comes from a COMMUNITY source "
            f"({entry.source}, publisher: {entry.publisher or 'unknown'}). "
            f"Review before trusting.[/yellow]"
        )
    if not entry.pinned:
        err_console.print(
            f"[yellow]⚠ {entry.name!r} is not version-pinned — its behavior can "
            f"change under you. Pin the entry if you can.[/yellow]"
        )

    # Best-effort reachability probe.
    if not no_inspect:
        from movate.core.skill_backend.base import SkillError  # noqa: PLC0415
        from movate.core.skill_backend.mcp import MCPSkillBackend  # noqa: PLC0415

        err_console.print(f"[dim]probing {entry.entry} …[/dim]")
        backend = MCPSkillBackend()

        async def _probe() -> list[dict[str, Any]]:
            # Single event loop for spawn + teardown (see inspect()).
            try:
                return await backend.discover_tools(entry.entry, entry.name, entry.credentials)
            finally:
                await backend.aclose()

        try:
            discovered = asyncio.run(_probe())
            console.print(f"[green]✓[/green] reachable — {len(discovered)} tool(s)")
        except SkillError as exc:
            err_console.print(
                f"[yellow]⚠ couldn't probe now ({exc.message}); writing the "
                f"declaration anyway (it'll discover at agent load).[/yellow]"
            )

    action = _add_server_to_yaml(target, server_entry)
    console.print(f"[green]✓[/green] {action} mcp_server [bold]{entry.name}[/bold] in {target}")
    if entry.credentials and entry.credentials.startswith("bearer-from-env:"):
        var = entry.credentials.removeprefix("bearer-from-env:")
        console.print(f"[dim]Set the credential before running: export {var}=…[/dim]")


def _add_server_to_yaml(yaml_path: Path, server_entry: dict[str, Any]) -> str:
    """Add/update an mcp_servers entry in a YAML file. Idempotent by name.

    Follows the repo's PyYAML round-trip convention (load → mutate → safe_dump
    with sort_keys=False), the same minimal-diff approach `mdk guardrails`
    uses. Returns "added" or "updated".
    """
    import yaml  # noqa: PLC0415

    raw = yaml.safe_load(yaml_path.read_text()) or {}
    servers = raw.get("mcp_servers") or []
    idx = next(
        (
            i
            for i, s in enumerate(servers)
            if isinstance(s, dict) and s.get("name") == server_entry["name"]
        ),
        None,
    )
    if idx is not None:
        servers[idx] = server_entry
        action = "updated"
    else:
        servers.append(server_entry)
        action = "added"
    raw["mcp_servers"] = servers
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return action


__all__ = ["mcp_app"]
