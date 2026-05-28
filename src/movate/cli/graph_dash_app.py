"""Dash + dash-cytoscape knowledge-graph viewer (the app module).

This is the Python-native production viewer option in the viz bake-off.
It renders the knowledge graph the runtime's graph API serves
(``GET /api/v1/projects/{id}/graph`` and friends, emitting graphology JSON)
as an interactive `dash-cytoscape` canvas — no JavaScript is authored.

The :func:`build_app` factory constructs a :class:`dash.Dash` app bound to
one runtime + one bearer token. **The bearer token lives only in this
server process** — it's captured in a server-side :class:`_GraphAPI` closure
and used to set the ``Authorization`` header on outbound API calls. It is
never placed in an ``elements`` payload, a ``dcc.Store``, a layout prop, or
any other value Dash serializes to the browser; the browser only ever sees
graph *data*, never the credential that fetched it.

Heavy deps (``dash``, ``dash-cytoscape``) are imported at *module top level*
here on purpose: this module is only ever imported *after* the CLI command
(``movate.cli.graph_dash_cmd``) has confirmed the ``graph-dash`` extra is
installed. Importing it without the extra raises ``ImportError`` — which the
command catches and turns into a friendly install hint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import dash_cytoscape as cyto
import httpx
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from movate.core.graph.cytoscape_format import (
    CytoscapeElement,
    graphology_to_cytoscape,
)

logger = logging.getLogger("movate.graph_dash")

# dash-cytoscape ships extra layout engines (cose-bilkent, cola, …) as
# loadable bundles. Register the responsive force-directed ones so the
# stylesheet can request ``cose`` (always available) or ``cola``.
cyto.load_extra_layouts()


# ---------------------------------------------------------------------------
# Server-side API client — holds the bearer token, never crosses to browser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GraphAPI:
    """Server-side graph-API client. Bound to one runtime + one bearer key.

    Lives entirely in the Dash *server* process. Every method issues a
    synchronous httpx request (Dash callbacks are sync) with the bearer
    token in the ``Authorization`` header. The token is a private field of
    this dataclass and is never returned to a callback or serialized into a
    Dash component — only the JSON *response bodies* (graph data) leave.
    """

    base_url: str
    _token: str
    project_id: str | None
    timeout: float = 15.0

    def _headers(self) -> dict[str, str]:
        # Built per-request, server-side only. Never logged.
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            resp = httpx.get(url, headers=self._headers(), params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except httpx.HTTPError as exc:
            # Log the failure WITHOUT the header (so the bearer never lands
            # in logs). The path is safe to log; the token is not.
            logger.warning("graph API GET %s failed: %s", path, type(exc).__name__)
            return {}

    def fetch_graph(self) -> dict[str, Any]:
        """``GET /api/v1/projects/{id}/graph`` — the windowed subgraph."""
        if self.project_id:
            return self._get(f"/api/v1/projects/{self.project_id}/graph")
        # No project id: fall back to a project-less graph endpoint if the
        # API exposes one. Empty graph is a valid render.
        return self._get("/api/v1/graph")

    def fetch_node(self, node_id: str) -> dict[str, Any]:
        """``GET /api/v1/graph/nodes/{id}`` — full node detail + provenance."""
        return self._get(f"/api/v1/graph/nodes/{node_id}")

    def fetch_neighbors(self, node_id: str) -> dict[str, Any]:
        """``GET /api/v1/graph/nodes/{id}/neighbors`` — one-hop expansion."""
        return self._get(f"/api/v1/graph/nodes/{node_id}/neighbors")

    def search(self, query: str) -> dict[str, Any]:
        """``GET /api/v1/graph/search?q=`` — name/type search."""
        return self._get("/api/v1/graph/search", params={"q": query})


# ---------------------------------------------------------------------------
# Stylesheet — color by type, size by degree, highlight/dim classes
# ---------------------------------------------------------------------------

# A small categorical palette. Cytoscape can't hash a string → color on its
# own, so we emit one selector per known type and let an explicit default
# catch the rest. Keeping this here (not in the adapter) keeps presentation
# out of the data layer.
_TYPE_PALETTE: dict[str, str] = {
    "Feature": "#4C78A8",
    "Policy": "#F58518",
    "Concept": "#54A24B",
    "System": "#E45756",
    "Person": "#72B7B2",
    "Org": "#B279A2",
    "Document": "#9D755D",
    "Event": "#EECA3B",
}
_DEFAULT_NODE_COLOR = "#8C8C8C"


def _build_stylesheet() -> list[dict[str, Any]]:
    """Cytoscape stylesheet: color by ``type``, size by ``degree``.

    ``mapData(degree, 0, 20, 18, 70)`` linearly maps a node's degree onto a
    diameter so hubs are visibly bigger. ``highlighted``/``dimmed`` classes
    are toggled by the tap callback to focus a neighborhood.
    """
    stylesheet: list[dict[str, Any]] = [
        {
            "selector": "node",
            "style": {
                "label": "data(label)",
                "font-size": "10px",
                "color": "#222",
                "text-valign": "center",
                "text-halign": "center",
                "background-color": _DEFAULT_NODE_COLOR,
                # Size by degree (clamped by mapData's domain).
                "width": "mapData(degree, 0, 20, 18, 70)",
                "height": "mapData(degree, 0, 20, 18, 70)",
            },
        },
        {
            "selector": "edge",
            "style": {
                "label": "data(label)",
                "font-size": "8px",
                "color": "#666",
                "curve-style": "bezier",
                "target-arrow-shape": "triangle",
                "target-arrow-color": "#bbb",
                "line-color": "#ccc",
                "width": 1.2,
            },
        },
        # Focus classes (toggled by the tap callback).
        {
            "selector": ".highlighted",
            "style": {
                "border-width": 3,
                "border-color": "#111",
                "opacity": 1,
            },
        },
        {
            "selector": ".dimmed",
            "style": {"opacity": 0.15},
        },
        {
            "selector": "edge.highlighted",
            "style": {"line-color": "#111", "target-arrow-color": "#111", "width": 2.5},
        },
    ]
    # One selector per known type → its color.
    for type_name, color in _TYPE_PALETTE.items():
        stylesheet.append(
            {
                "selector": f'node[type = "{type_name}"]',
                "style": {"background-color": color},
            }
        )
    return stylesheet


def _node_types(elements: list[CytoscapeElement]) -> list[str]:
    """Sorted distinct node types present in ``elements`` (for the filter)."""
    types = {str(el["data"].get("type", "")) for el in elements if "source" not in el["data"]}
    return sorted(t for t in types if t)


def _merge_elements(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge ``incoming`` elements into ``existing``, de-duping by identity.

    Nodes de-dupe on ``data.id``; edges on ``(source, target, label)`` (an
    edge has no stable id across windows). Used by the *expand* drill so a
    neighbor fetch grows the canvas instead of replacing it.
    """
    seen_nodes = {
        el["data"]["id"] for el in existing if "id" in el["data"] and "source" not in el["data"]
    }
    seen_edges = {
        (el["data"].get("source"), el["data"].get("target"), el["data"].get("label"))
        for el in existing
        if "source" in el["data"]
    }
    merged = list(existing)
    for el in incoming:
        data = el["data"]
        if "source" in data:
            sig = (data.get("source"), data.get("target"), data.get("label"))
            if sig not in seen_edges:
                seen_edges.add(sig)
                merged.append(el)
        else:
            node_id = data.get("id")
            if node_id is not None and node_id not in seen_nodes:
                seen_nodes.add(node_id)
                merged.append(el)
    return merged


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app(
    *,
    base_url: str,
    bearer_token: str,
    project_id: str | None,
    poll_live: bool = False,
) -> Dash:
    """Construct the Dash app bound to one runtime + bearer token.

    ``bearer_token`` is captured in the :class:`_GraphAPI` instance — a
    server-side closure — and never enters the layout or any ``dcc.Store``.
    The browser receives only graph data.

    ``poll_live`` enables the optional live-growth poll (a ``dcc.Interval``
    that re-fetches the windowed graph and merges in new nodes). Off by
    default; toggled by ``--live`` on the command.
    """
    api = _GraphAPI(base_url=base_url, _token=bearer_token, project_id=project_id)

    # Initial fetch (server-side). If the API is unreachable, render an empty
    # canvas rather than crash — the side panel surfaces the error state.
    initial_graph = api.fetch_graph()
    initial_elements = graphology_to_cytoscape(initial_graph)
    type_options = [{"label": t, "value": t} for t in _node_types(initial_elements)]
    type_values = [opt["value"] for opt in type_options]

    app = Dash(__name__, title="MDK Knowledge Graph (Dash)")
    # Note: the server-side ``api`` client is captured by the callbacks
    # registered below (a closure), so the bearer token never needs to be
    # stashed on the app or in any browser-visible component.

    cyto_graph = cyto.Cytoscape(
        id="kg-cytoscape",
        elements=initial_elements,
        layout={"name": "cose", "animate": False},
        stylesheet=_build_stylesheet(),
        style={"width": "100%", "height": "82vh"},
        minZoom=0.2,
        maxZoom=3.0,
    )

    controls = html.Div(
        [
            dcc.Input(
                id="kg-search",
                type="text",
                placeholder="Search nodes…",
                debounce=True,
                style={"width": "180px", "marginRight": "8px"},
            ),
            html.Button("Search", id="kg-search-btn", n_clicks=0),
            html.Div(
                dcc.Checklist(
                    id="kg-type-filter",
                    options=type_options,
                    value=type_values,
                    inline=True,
                ),
                style={"marginTop": "8px"},
            ),
        ],
        style={"padding": "8px"},
    )

    side_panel = html.Div(
        id="kg-side-panel",
        children=html.Div(
            "Tap a node to highlight its neighborhood; "
            "double-tap (or use Details) to load provenance.",
            style={"color": "#888"},
        ),
        style={
            "width": "320px",
            "padding": "12px",
            "borderLeft": "1px solid #eee",
            "overflowY": "auto",
            "height": "82vh",
        },
    )

    children: list[Any] = [
        html.H3("MDK Knowledge Graph", style={"margin": "8px"}),
        controls,
        html.Div(
            [
                html.Div(cyto_graph, style={"flex": "1"}),
                side_panel,
            ],
            style={"display": "flex"},
        ),
        # All cross-callback state is graph *data* or a node *id* — never the
        # bearer token. Stores live in the browser; the token does not.
        dcc.Store(id="kg-all-elements", data=initial_elements),
        dcc.Store(id="kg-selected-node", data=None),
    ]
    if poll_live:
        children.append(dcc.Interval(id="kg-live-poll", interval=10_000, n_intervals=0))

    app.layout = html.Div(children)

    _register_callbacks(app, api, poll_live=poll_live)
    return app


def _register_callbacks(app: Dash, api: _GraphAPI, *, poll_live: bool) -> None:
    """Wire the interaction callbacks. ``api`` is the server-side client."""

    # --- Tap a node → highlight it + its neighbors, dim the rest ----------
    @app.callback(
        Output("kg-cytoscape", "stylesheet"),
        Output("kg-selected-node", "data"),
        Input("kg-cytoscape", "tapNodeData"),
        State("kg-all-elements", "data"),
        prevent_initial_call=True,
    )
    def _highlight_neighborhood(
        tapped: dict[str, Any] | None, elements: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str | None]:
        base = _build_stylesheet()
        if not tapped:
            return base, None
        node_id = tapped.get("id")
        if not node_id:
            return base, None
        neighbors = {node_id}
        for el in elements or []:
            data = el["data"]
            if "source" in data:
                if data.get("source") == node_id:
                    neighbors.add(data.get("target"))
                elif data.get("target") == node_id:
                    neighbors.add(data.get("source"))
        focus = [
            *base,
            {"selector": "node", "style": {"opacity": 0.15}},
            {"selector": "edge", "style": {"opacity": 0.08}},
        ]
        for nid in neighbors:
            focus.append({"selector": f'node[id = "{nid}"]', "style": {"opacity": 1}})
        focus.append(
            {
                "selector": f'node[id = "{node_id}"]',
                "style": {"border-width": 3, "border-color": "#111", "opacity": 1},
            }
        )
        return focus, node_id

    # --- Details: tap or double-tap → fetch node detail + provenance ------
    @app.callback(
        Output("kg-side-panel", "children"),
        Input("kg-cytoscape", "tapNodeData"),
        prevent_initial_call=True,
    )
    def _show_details(tapped: dict[str, Any] | None) -> Any:
        if not tapped or not tapped.get("id"):
            return no_update
        detail = api.fetch_node(str(tapped["id"]))
        return _render_side_panel(tapped, detail)

    # --- Expand: merge a node's neighbors into the canvas -----------------
    @app.callback(
        Output("kg-cytoscape", "elements"),
        Output("kg-all-elements", "data"),
        Input("kg-expand-btn", "n_clicks"),
        State("kg-selected-node", "data"),
        State("kg-all-elements", "data"),
        State("kg-type-filter", "value"),
        prevent_initial_call=True,
    )
    def _expand(
        n_clicks: int | None,
        node_id: str | None,
        elements: list[dict[str, Any]],
        type_filter: list[str] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not n_clicks or not node_id:
            return no_update, no_update
        neighbor_graph = api.fetch_neighbors(node_id)
        incoming = graphology_to_cytoscape(neighbor_graph)
        merged = _merge_elements(elements or [], incoming)
        return _apply_type_filter(merged, type_filter), merged

    # --- Filter by node type ---------------------------------------------
    @app.callback(
        Output("kg-cytoscape", "elements", allow_duplicate=True),
        Input("kg-type-filter", "value"),
        State("kg-all-elements", "data"),
        prevent_initial_call=True,
    )
    def _filter(
        selected_types: list[str] | None, elements: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return _apply_type_filter(elements or [], selected_types)

    # --- Search: select + center the matched node ------------------------
    @app.callback(
        Output("kg-cytoscape", "elements", allow_duplicate=True),
        Output("kg-cytoscape", "stylesheet", allow_duplicate=True),
        Input("kg-search-btn", "n_clicks"),
        Input("kg-search", "value"),
        State("kg-all-elements", "data"),
        State("kg-type-filter", "value"),
        prevent_initial_call=True,
    )
    def _search(
        n_clicks: int | None,
        query: str | None,
        elements: list[dict[str, Any]],
        type_filter: list[str] | None,
    ) -> tuple[Any, Any]:
        if not query or not query.strip():
            return no_update, no_update
        result = api.search(query.strip())
        matched = graphology_to_cytoscape(result)
        merged = _merge_elements(elements or [], matched)
        filtered = _apply_type_filter(merged, type_filter)
        # Highlight the first match.
        match_ids = [el["data"]["id"] for el in matched if "source" not in el["data"]]
        style = _build_stylesheet()
        if match_ids:
            style = [
                *style,
                {"selector": "node", "style": {"opacity": 0.2}},
                {
                    "selector": f'node[id = "{match_ids[0]}"]',
                    "style": {
                        "opacity": 1,
                        "border-width": 4,
                        "border-color": "#d62728",
                    },
                },
            ]
        return filtered, style

    # --- Optional live-growth poll ---------------------------------------
    if poll_live:

        @app.callback(
            Output("kg-cytoscape", "elements", allow_duplicate=True),
            Output("kg-all-elements", "data", allow_duplicate=True),
            Input("kg-live-poll", "n_intervals"),
            State("kg-all-elements", "data"),
            State("kg-type-filter", "value"),
            prevent_initial_call=True,
        )
        def _poll(
            _n: int,
            elements: list[dict[str, Any]],
            type_filter: list[str] | None,
        ) -> tuple[Any, Any]:
            latest = graphology_to_cytoscape(api.fetch_graph())
            merged = _merge_elements(elements or [], latest)
            if len(merged) == len(elements or []):
                return no_update, no_update
            return _apply_type_filter(merged, type_filter), merged

    # ``ctx`` is referenced so linters keep the import; multi-trigger
    # disambiguation can use it as the viewer grows.
    _ = ctx


def _apply_type_filter(
    elements: list[dict[str, Any]], selected_types: list[str] | None
) -> list[dict[str, Any]]:
    """Keep nodes whose type is selected + edges between surviving nodes.

    ``selected_types is None`` means "no filter applied yet" → show all.
    An empty list means "everything deselected" → show nothing.
    """
    if selected_types is None:
        return elements
    keep_types = set(selected_types)
    kept_node_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for el in elements:
        data = el["data"]
        if "source" not in data and str(data.get("type", "")) in keep_types:
            kept_node_ids.add(data["id"])
            out.append(el)
    for el in elements:
        data = el["data"]
        if (
            "source" in data
            and data.get("source") in kept_node_ids
            and data.get("target") in kept_node_ids
        ):
            out.append(el)
    return out


def _render_side_panel(tapped: dict[str, Any], detail: dict[str, Any]) -> Any:
    """Render the node detail side panel: properties + provenance + agents.

    ``detail`` is the ``GET /api/v1/graph/nodes/{id}`` body. We render
    defensively — any field can be absent on a partial response.
    """
    attrs = detail.get("attributes") if isinstance(detail, dict) else None
    attrs = attrs if isinstance(attrs, dict) else {}

    label = attrs.get("label") or attrs.get("name") or tapped.get("label") or tapped.get("id")
    node_type = attrs.get("type") or tapped.get("type") or "—"

    # Properties block (scalar attrs only).
    prop_rows = [
        html.Tr([html.Td(html.B(k)), html.Td(str(v))])
        for k, v in attrs.items()
        if isinstance(v, (str, int, float, bool)) and k not in {"label", "name", "type"}
    ]

    # Provenance: source chunk + URL + confidence.
    provenance = detail.get("provenance") or attrs.get("provenance") or []
    prov_children: list[Any] = []
    if isinstance(provenance, list) and provenance:
        for prov in provenance:
            if not isinstance(prov, dict):
                continue
            src = prov.get("source") or prov.get("chunk_id") or "source"
            url = prov.get("url")
            conf = prov.get("confidence")
            line: list[Any] = [
                html.A(str(src), href=str(url), target="_blank") if url else html.Span(str(src))
            ]
            if conf is not None:
                line.append(html.Span(f"  (confidence {conf})", style={"color": "#888"}))
            prov_children.append(html.Li(line))
    else:
        prov_children.append(html.Li("No provenance recorded.", style={"color": "#888"}))

    # Referenced-by-agents.
    agents = detail.get("referenced_by_agents") or attrs.get("referenced_by_agents") or []
    agent_children = (
        [html.Li(str(a)) for a in agents]
        if isinstance(agents, list) and agents
        else [html.Li("No referencing agents.", style={"color": "#888"})]
    )

    return html.Div(
        [
            html.H4(str(label)),
            html.Div(f"Type: {node_type}", style={"color": "#666", "marginBottom": "8px"}),
            html.Button(
                "Expand neighbors",
                id="kg-expand-btn",
                n_clicks=0,
                style={"marginBottom": "12px"},
            ),
            html.H5("Properties"),
            html.Table(prop_rows) if prop_rows else html.Div("—", style={"color": "#888"}),
            html.H5("Provenance"),
            html.Ul(prov_children),
            html.H5("Referenced by agents"),
            html.Ul(agent_children),
        ]
    )
