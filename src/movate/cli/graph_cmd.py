"""``mdk graph`` — inspect a deployed agent's knowledge graph (ADR 046).

A thin, **read-only** CLI over the runtime's graphology graph query API:

* ``mdk graph show <project> --target <env>`` — fetch a windowed subgraph
  (``GET /api/v1/projects/<project>/graph``) and print a summary table.
  ``--json`` emits the raw graphology document (zero-transform — pipe it
  straight into a sigma.js importer or jq).
* ``mdk graph node <node_id> --target <env>`` — fetch one node's detail +
  provenance (``GET /api/v1/graph/nodes/<id>``).

Both are remote-only (``--target`` required): the graph lives in the
deployed runtime's storage. This mirrors the ``mdk kb --target`` paths and
reuses their target-resolution + bearer plumbing (no duplicate auth
logic). The command never writes — extraction + ingest stay with
``mdk kb ingest --build-graph``.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

graph_app = typer.Typer(
    name="graph",
    help="Inspect a deployed agent's knowledge graph (read-only, graphology JSON).",
    no_args_is_help=True,
)

# HTTP status codes used by the remote paths (mirrors kb_cmd).
_HTTP_OK = 200
_HTTP_REDIRECT = 300
_HTTP_UNAUTHORIZED = 401
_HTTP_NOT_FOUND = 404


def _graph_remote_get(
    *,
    target: str,
    path: str,
    params: dict[str, Any] | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """GET an authenticated runtime graph endpoint; return the JSON body.

    Reuses :func:`movate.cli.kb_cmd._resolve_target_bearer` for
    target → URL → bearer resolution (one place owns that logic). Drops
    ``None`` query params so an unset filter doesn't serialize as an empty
    string. Translates the runtime failure surface into actionable CLI
    errors (exit 2): 401 → refresh hint, 404 → not-found, other non-2xx →
    raw status + truncated body, network error → unreachable message.
    """
    import httpx  # noqa: PLC0415

    from movate.cli.kb_cmd import _resolve_target_bearer  # noqa: PLC0415

    target_name, target_cfg, base_url, api_key = _resolve_target_bearer(target)
    endpoint = f"{base_url}{path}"
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
            resp = client.get(endpoint, params=clean_params or None, headers=headers)
    except httpx.HTTPError as exc:
        err_console.print(f"[red]✗[/red] could not reach {base_url}: {exc}")
        raise typer.Exit(code=2) from None

    if resp.status_code == _HTTP_UNAUTHORIZED:
        err_console.print(
            f"[red]✗[/red] runtime rejected the bearer (${target_cfg.key_env}). "  # type: ignore[attr-defined]
            f"Refresh it: [bold]mdk auth refresh-runtime-key {target_name}[/bold]."
        )
        raise typer.Exit(code=2)
    if resp.status_code == _HTTP_NOT_FOUND:
        err_console.print(f"[red]✗[/red] not found on [bold]{target_name}[/bold]: {path}")
        raise typer.Exit(code=2)
    if not (_HTTP_OK <= resp.status_code < _HTTP_REDIRECT):
        err_console.print(f"[red]✗[/red] HTTP {resp.status_code}: {resp.text[:200]}")
        raise typer.Exit(code=2)

    body = resp.json() if resp.content else {}
    return body if isinstance(body, dict) else {}


@graph_app.command("show")
def show(
    project: str = typer.Argument(..., help="Agent whose graph to fetch."),
    target: str = typer.Option(
        ...,
        "--target",
        help="Deployed runtime to query (resolves URL + bearer from ~/.movate/config.yaml).",
    ),
    mode: str = typer.Option(
        "knowledge",
        "--mode",
        help="Graph mode: knowledge | topology.",
    ),
    type: str | None = typer.Option(
        None,
        "--type",
        help="Filter to one node type.",
    ),
    root: str | None = typer.Option(
        None,
        "--root",
        help="Center the window on a node id (bounded k-hop expansion).",
    ),
    depth: int | None = typer.Option(
        None,
        "--depth",
        min=1,
        help="Hops from --root (server caps at 6).",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Node/edge cap (server default 500, max 5000).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the raw graphology document (zero-transform sigma.js import).",
    ),
) -> None:
    """Fetch ``project``'s knowledge graph as graphology JSON.

    Prints a summary table by default; ``--json`` emits the graphology
    document verbatim so a client (or ``jq``) consumes it with no
    transform.
    """
    doc = _graph_remote_get(
        target=target,
        path=f"/api/v1/projects/{project}/graph",
        params={
            "mode": mode,
            "type": type,
            "root": root,
            "depth": depth,
            "limit": limit,
        },
    )

    if json_out:
        # Raw graphology document straight to stdout — machine path.
        console.print_json(json.dumps(doc))
        return

    nodes = doc.get("nodes", []) or []
    edges = doc.get("edges", []) or []
    if not nodes:
        err_console.print(
            f"[yellow]⚠[/yellow] no graph for [bold]{project}[/bold] (mode=[bold]{mode}[/bold]). "
            "Build one with [bold]mdk kb ingest <agent> <path> --build-graph[/bold]."
        )
        return

    table = Table(
        title=(
            f"[bold]Knowledge graph[/bold] — [bold]{project}[/bold] "
            f"[dim]({len(nodes)} nodes, {len(edges)} edges)[/dim]"
        ),
        show_lines=False,
    )
    table.add_column("node id", overflow="fold", max_width=34, style="dim", no_wrap=True)
    table.add_column("label", overflow="fold")
    table.add_column("type", no_wrap=True)
    table.add_column("size", justify="right", style="dim", no_wrap=True)
    for n in nodes:
        attrs = n.get("attributes", {}) or {}
        table.add_row(
            str(n.get("key", "")),
            str(attrs.get("label", "")),
            str(attrs.get("type", "")),
            str(attrs.get("size", "")),
        )
    console.print(table)


@graph_app.command("node")
def node(
    node_id: str = typer.Argument(..., help="Graph node id to inspect."),
    target: str = typer.Option(
        ...,
        "--target",
        help="Deployed runtime to query (resolves URL + bearer from ~/.movate/config.yaml).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Scope the lookup to one agent's graph.",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit the raw node-detail JSON.",
    ),
) -> None:
    """Fetch one node's detail + provenance (source chunks + url + confidence)."""
    detail = _graph_remote_get(
        target=target,
        path=f"/api/v1/graph/nodes/{node_id}",
        params={"project": project},
    )

    if json_out:
        console.print_json(json.dumps(detail))
        return

    console.print(
        f"[bold]{detail.get('label', '?')}[/bold] "
        f"[dim]({detail.get('type', '?')})[/dim]  "
        f"[dim]{detail.get('key', '')}[/dim]"
    )
    description = detail.get("description")
    if description:
        console.print(f"  {description}")
    console.print(
        f"  neighbors: [bold]{detail.get('neighbor_count', 0)}[/bold]   "
        f"referenced by: {', '.join(detail.get('referenced_by_agents', []) or []) or '—'}"
    )

    provenance = detail.get("provenance", []) or []
    if provenance:
        table = Table(title="[bold]Provenance[/bold]", show_lines=True)
        table.add_column("chunk id", overflow="fold", max_width=24, style="dim", no_wrap=True)
        table.add_column("source", overflow="fold", max_width=36)
        table.add_column("conf", justify="right", style="dim", no_wrap=True)
        table.add_column("snippet", overflow="fold")
        for p in provenance:
            conf = p.get("extraction_confidence")
            table.add_row(
                str(p.get("chunk_id", "")),
                str(p.get("url") or "—"),
                f"{conf:.2f}" if isinstance(conf, (int, float)) else "—",
                str(p.get("snippet") or "—"),
            )
        console.print(table)
