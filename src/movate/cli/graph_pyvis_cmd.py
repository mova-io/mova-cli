"""``mdk graph export`` — standalone interactive HTML graph viewer (PyVis).

The lightweight, **air-gapped + shareable** option in the graph-viz line-up.
Where ``mdk graph serve`` (sigma.js, ADR 046 D8) stands up a live WebGL viewer
and the Dash exporter renders a server-backed dashboard, this command fetches a
windowed subgraph from the graph query API (ADR 046 D5) and writes a **single
self-contained HTML file** — drag/zoom/hover/click interactivity baked in via
vis.js, no server, no network calls from the artifact. Open it in any browser
or email it to a stakeholder.

Quickstart::

    # Install the optional extra (PyVis + NetworkX + Jinja2 — all BSD)
    uv pip install 'movate-cli[graph-pyvis]'

    # Export the active target's project graph to a standalone file
    mdk graph export knowledge.html --target prod --open

How the artifact stays safe + shareable
---------------------------------------

* **Through-the-API.** The graph is fetched server-side, in *this* CLI process,
  using the target's bearer key (resolved from its ``key_env``). The key is
  used only to authenticate the fetch.
* **The bearer key is NEVER written into the HTML.** The exported file contains
  only graph *data* (nodes, edges, layout). It is a static snapshot — it makes
  no callbacks to the API, so there is no auth concern in the artifact itself.
* **Self-contained / air-gapped.** PyVis is asked for ``cdn_resources="in_line"``
  so the vis.js assets are inlined into the HTML. The file works fully offline
  and is a single emailable attachment — no CDN fetch, no internet required.

This is a control-plane (CLI) command only; per the layer rules it owns its I/O
and the remote fetch, and delegates the pure graphology→NetworkX shape mapping
to :mod:`movate.core.graph.networkx_format`.
"""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from movate.cli._console import error, get_global_target, hint, success
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)

err = Console(stderr=True)

# Default export window — bounded per ADR 046 D5/R6 (never ship the whole
# graph). Operators can widen via flags.
_DEFAULT_LIMIT = 500
_DEFAULT_DEPTH = 2


graph_app = typer.Typer(
    name="graph",
    help=(
        "Knowledge-graph viewers + exporters. `export` writes a standalone, "
        "air-gapped, shareable interactive HTML file (PyVis)."
    ),
    no_args_is_help=True,
)


def _ensure_pyvis_installed() -> None:
    """Friendly error + clean exit when the optional ``graph-pyvis`` extra is absent.

    PyVis (and its NetworkX / Jinja2 deps) are not part of the core install —
    only operators exporting a graph need them. A clear install hint here beats
    the cryptic ``ModuleNotFoundError`` the import would otherwise raise.
    """
    try:
        import pyvis  # noqa: F401, PLC0415
    except ImportError:
        err.print(
            "[red]✗[/red] [bold]pyvis[/bold] not installed. "
            "The standalone HTML graph exporter is gated behind an optional "
            "extra to keep the default install size down.\n\n"
            "Install with:\n  "
            "[bold]uv pip install 'movate-cli\\[graph-pyvis]'[/bold]\n\n"
            "Or, if you installed mdk as a uv tool:\n  "
            "[bold]uv tool install 'movate-cli\\[graph-pyvis]'[/bold]\n\n"
            "Or, for a development install of this repo:\n  "
            "[bold]uv sync --extra graph-pyvis[/bold]"
        )
        raise typer.Exit(code=2) from None


@graph_app.command("export")
def export(
    output: Path = typer.Argument(
        ...,
        metavar="OUTPUT.html",
        help="Path to write the standalone interactive HTML file.",
    ),
    target: str = typer.Option(
        None,
        "--target",
        "-t",
        help=(
            "Deployment target name (from `mdk config list-targets`). "
            "Omit to use the active target. The graph is fetched from this "
            "runtime's graph API using its bearer key."
        ),
    ),
    project: str = typer.Option(
        None,
        "--project",
        "-p",
        help=(
            "Project id to export the graph for. Omit to use the target's "
            "default project (the server resolves it from the caller's scope)."
        ),
    ),
    limit: int = typer.Option(
        _DEFAULT_LIMIT,
        "--limit",
        help="Max nodes to fetch (bounded window — never the whole graph).",
    ),
    depth: int = typer.Option(
        _DEFAULT_DEPTH,
        "--depth",
        help="Neighbourhood expansion depth (hops) for a richer export.",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open",
        help="Open the generated HTML in the default browser when done.",
    ),
) -> None:
    """Fetch a graph and write a standalone interactive HTML viewer.

    [bold]Examples:[/bold]

      [dim]# Export the active target's project graph[/dim]
      $ mdk graph export knowledge.html

      [dim]# A specific project on prod, wider window, then open it[/dim]
      $ mdk graph export kg.html -t prod -p proj-42 --limit 1000 --open

    The output is a single self-contained file (vis.js inlined): it works
    offline / air-gapped and is emailable. It contains only graph DATA — the
    bearer key used to fetch it is never written into the artifact.
    """
    # Fail fast on the missing extra BEFORE doing any network work, so the
    # operator gets the install hint immediately.
    _ensure_pyvis_installed()

    try:
        # Per-command --target wins; otherwise the process-wide default
        # (`mdk -t <name>` / MOVATE_TARGET); otherwise the active config target.
        target_name, target_cfg = resolve_target(target or get_global_target())
        token = resolve_bearer_token(target_cfg)
    except UserConfigError as exc:
        error(str(exc))
        raise typer.Exit(code=2) from None

    try:
        graphology = asyncio.run(
            _fetch_graph(
                base_url=target_cfg.url,
                token=token,
                project=project,
                limit=limit,
                depth=depth,
            )
        )
    except _GraphFetchError as exc:
        error(str(exc), context="graph export")
        raise typer.Exit(code=1) from None

    node_count = len(graphology.get("nodes") or [])
    edge_count = len(graphology.get("edges") or [])
    hint(f"[dim]fetched {node_count} nodes / {edge_count} edges from {target_name}[/dim]")

    try:
        out_path = _write_html(graphology, output=output)
    except OSError as exc:
        error(f"could not write {output}: {exc}", context="graph export")
        raise typer.Exit(code=1) from None

    success(f"wrote standalone graph viewer → {out_path} ({node_count} nodes, {edge_count} edges)")
    hint("[dim]self-contained (vis.js inlined): open offline or email it.[/dim]")

    if open_browser:
        webbrowser.open(out_path.resolve().as_uri())


# ---------------------------------------------------------------------------
# Remote fetch (through-the-API; bearer key stays server-side)
# ---------------------------------------------------------------------------


class _GraphFetchError(Exception):
    """Raised when the graph API fetch fails — translated to a CLI error."""


async def _fetch_graph(
    *,
    base_url: str,
    token: str,
    project: str | None,
    limit: int,
    depth: int,
) -> dict[str, Any]:
    """GET the windowed subgraph (graphology JSON) from the graph query API.

    Uses the bearer key only to authenticate the request (``Authorization:
    Bearer``). The key never leaves this process and is never written to the
    exported artifact.

    The endpoint follows ADR 046 D5: project-scoped when a project id is given,
    otherwise the caller's default-project graph. ``limit`` + ``depth`` bound
    the window so we never pull the whole graph.
    """
    import httpx  # noqa: PLC0415 — httpx is a core dep; lazy import keeps CLI startup light

    # With a project id, the path is project-scoped (ADR 046 D5); without one,
    # the server resolves the default project from the caller's scope.
    path = f"/api/v1/projects/{project}/graph" if project else "/api/v1/graph"
    params = {"limit": limit, "depth": depth}

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as client:
        try:
            resp = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise _GraphFetchError(f"request to {base_url} failed: {exc}") from exc

    if not resp.is_success:
        # Mirror MovateClient's error-envelope parsing for a readable message.
        message = f"HTTP {resp.status_code}"
        try:
            payload = resp.json()
            envelope = (
                payload.get("detail", {}).get("error", {}) if isinstance(payload, dict) else {}
            )
            message = envelope.get("message", message)
        except ValueError:
            pass
        raise _GraphFetchError(message)

    try:
        data = resp.json()
    except ValueError as exc:
        raise _GraphFetchError(f"response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _GraphFetchError("graph API returned an unexpected (non-object) payload")
    return data


# ---------------------------------------------------------------------------
# HTML generation (PyVis — standalone, in-line assets)
# ---------------------------------------------------------------------------


def _write_html(graphology: dict[str, Any], *, output: Path) -> Path:
    """Build a PyVis network from graphology JSON and write standalone HTML.

    * ``cdn_resources="in_line"`` inlines vis.js into the file → air-gapped,
      single-file, emailable.
    * A small colour→type legend is injected so the viewer is self-explaining.
    * The artifact is data-only; no bearer key is ever written into it.
    """
    from pyvis.network import Network  # noqa: PLC0415 — gated behind the extra

    from movate.core.graph.networkx_format import (  # noqa: PLC0415
        build_type_color_map,
        graphology_to_networkx,
    )

    nx_graph = graphology_to_networkx(graphology)

    net = Network(
        height="100vh",
        width="100%",
        directed=False,
        notebook=False,
        # The load-bearing flag for the air-gapped + shareable property: inline
        # vis.js assets so the file works offline and is a single attachment.
        cdn_resources="in_line",
    )
    # Honour any persisted server-side layout (ADR 046 D4); physics still lets
    # the operator drag nodes around interactively.
    net.from_nx(nx_graph)
    net.toggle_physics(True)

    html = net.generate_html(notebook=False)
    html = _inject_legend(html, nx_graph, color_map_fn=build_type_color_map)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output


def _inject_legend(html: str, nx_graph: Any, *, color_map_fn: Any) -> str:
    """Inject a small colour→node-type legend into the generated HTML.

    A floating, fixed-position box (top-left) listing each node ``type`` with
    its swatch colour — so the standalone file is self-explaining without the
    API or any external context.
    """
    import html as html_escaper  # noqa: PLC0415

    types = [str(data.get("type", "")) for _, data in nx_graph.nodes(data=True)]
    color_map = color_map_fn(types)
    if not color_map:
        return html

    rows = "".join(
        f'<div style="display:flex;align-items:center;margin:2px 0">'
        f'<span style="display:inline-block;width:12px;height:12px;border-radius:2px;'
        f'background:{color};margin-right:6px"></span>'
        f"<span>{html_escaper.escape(type_)}</span></div>"
        for type_, color in sorted(color_map.items())
    )
    legend = (
        '<div style="position:fixed;top:12px;left:12px;z-index:1000;'
        "background:rgba(255,255,255,0.92);border:1px solid #ccc;border-radius:6px;"
        "padding:8px 10px;font-family:sans-serif;font-size:12px;"
        'box-shadow:0 1px 4px rgba(0,0,0,0.2)">'
        '<div style="font-weight:bold;margin-bottom:4px">Node types</div>'
        f"{rows}</div>"
    )
    # Inject just inside <body> so it floats over the canvas.
    marker = "<body>"
    if marker in html:
        return html.replace(marker, marker + legend, 1)
    return legend + html
