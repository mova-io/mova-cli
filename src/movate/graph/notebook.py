"""ipysigma notebook helper for the knowledge graph.

The data-science / Jupyter viewer option in the graph-viz bake-off. For
analysts (and Deva) who live in notebooks: fetch a (sub)graph from the
graph query API, adapt it to NetworkX, and render it as an interactive
sigma.js widget inline.

Public API
----------

* :func:`load_graph` — fetch the graph API for a target/project, adapt the
  graphology JSON to a :class:`networkx.Graph`.
* :func:`show_graph` — thin wrapper over ``ipysigma.Sigma(...)`` with
  sensible knowledge-graph defaults (color by ``type``, size by degree,
  label from ``label``, edge weight from ``weight``). Returns the widget.
* :func:`node_detail` — fetch ``/graph/nodes/{id}`` for the provenance /
  properties of a clicked node, so the notebook user can drill in.

Dependency discipline
----------------------

``networkx`` and ``ipysigma`` are OPT-IN (the ``graph-notebook`` extra).
They are imported lazily inside the functions that need them, raising a
friendly install hint (and a clean ``ImportError``) when absent — so
``import movate.graph.notebook`` never requires them. ``httpx`` is a core
dependency and is used directly to talk to the graph API.

Security
--------

These helpers take an explicit ``api_key`` argument. They NEVER read,
log, or persist it beyond the single Authorization header on the wire,
and they talk to the runtime API only (no direct Postgres). The CLI that
generates a notebook reads the key from ``os.environ["MOVATE_API_KEY"]``
at runtime and never bakes it into the file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from movate.core.graph.networkx_format import (
    _INSTALL_HINT,
    GraphFormatError,
    graphology_to_networkx,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import networkx as nx

# Default timeout for graph-API calls. Subgraph fetches can be chunky;
# 30s matches the runtime client's default and is overridable per call.
_DEFAULT_TIMEOUT = 30.0


def _require_ipysigma() -> Any:
    """Lazily import ipysigma, raising a friendly hint if it's absent."""
    try:
        from ipysigma import Sigma  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return Sigma


def _normalize_base_url(base_url: str) -> str:
    """Strip a trailing slash so path joins stay single-slashed."""
    return base_url.rstrip("/")


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def load_graph(
    target: str,
    project_id: str,
    *,
    base_url: str,
    api_key: str,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> nx.Graph:
    """Fetch a (sub)graph from the graph API and adapt it to NetworkX.

    Calls ``GET {base_url}/graph`` with ``target`` (deployment env, e.g.
    ``"prod"``) and ``project_id`` as query params, expecting a graphology
    JSON body, and converts it via
    :func:`movate.core.graph.networkx_format.graphology_to_networkx`.

    Args:
        target: Deployment target/env the graph belongs to (query param).
        project_id: Project whose graph to fetch (query param).
        base_url: Runtime API base URL (trailing slash optional).
        api_key: Bearer token for the runtime. Sent only as an
            ``Authorization`` header; never logged or persisted.
        timeout: Per-request timeout in seconds.
        transport: Optional httpx transport (tests inject a
            ``MockTransport``); ``None`` uses the default network transport.

    Returns:
        A :class:`networkx.Graph` (or ``DiGraph`` for directed graphs).

    Raises:
        ImportError: if the ``graph-notebook`` extra (networkx) is absent.
        httpx.HTTPStatusError: on a non-2xx response from the graph API.
        GraphFormatError: if the response body isn't valid graphology JSON.
    """
    url = f"{_normalize_base_url(base_url)}/graph"
    with httpx.Client(transport=transport, timeout=timeout) as client:
        response = client.get(
            url,
            params={"target": target, "project_id": project_id},
            headers=_auth_headers(api_key),
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise GraphFormatError(
            f"graph API returned {type(payload).__name__}, expected a JSON object"
        )
    return graphology_to_networkx(payload)


def show_graph(graph: nx.Graph, **sigma_kwargs: Any) -> Any:
    """Render ``graph`` as an interactive ipysigma widget in a notebook.

    Thin wrapper over ``ipysigma.Sigma`` with sensible knowledge-graph
    defaults; any ``sigma_kwargs`` override or extend them:

    * ``node_color="type"`` — color nodes by their ``type`` attribute.
    * ``node_size`` — by degree (ipysigma's ``"degree"`` metric).
    * ``node_label="label"`` — label from the ``label`` attribute.
    * ``edge_weight="weight"`` — edge thickness from the ``weight`` attr.

    Returns the ``Sigma`` widget; display it by making it the last
    expression in a notebook cell.

    Raises:
        ImportError: if the ``graph-notebook`` extra (ipysigma) is absent.
    """
    Sigma = _require_ipysigma()  # noqa: N806 - imported class, keep its name
    defaults: dict[str, Any] = {
        "node_color": "type",
        "node_size": "degree",
        "node_label": "label",
        "edge_weight": "weight",
    }
    defaults.update(sigma_kwargs)
    return Sigma(graph, **defaults)


def node_detail(
    node_id: str,
    *,
    base_url: str,
    api_key: str,
    timeout: float = _DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch the full record for a single node — the drill-in for a click.

    Calls ``GET {base_url}/graph/nodes/{node_id}`` and returns the parsed
    JSON: the node's properties + provenance the graph API carries (the
    summary node attributes in the rendered widget are intentionally
    lightweight; this is how a notebook user inspects the rest).

    Args:
        node_id: The graphology node ``key`` (e.g. from a clicked node).
        base_url: Runtime API base URL (trailing slash optional).
        api_key: Bearer token; sent only as an Authorization header.
        timeout: Per-request timeout in seconds.
        transport: Optional httpx transport for tests.

    Returns:
        The node record as a ``dict``.

    Raises:
        httpx.HTTPStatusError: on a non-2xx response (e.g. 404 unknown id).
    """
    url = f"{_normalize_base_url(base_url)}/graph/nodes/{node_id}"
    with httpx.Client(transport=transport, timeout=timeout) as client:
        response = client.get(url, headers=_auth_headers(api_key))
        response.raise_for_status()
        detail = response.json()
    if not isinstance(detail, dict):
        raise GraphFormatError(
            f"node detail API returned {type(detail).__name__}, expected a JSON object"
        )
    return detail


__all__ = [
    "load_graph",
    "node_detail",
    "show_graph",
]
