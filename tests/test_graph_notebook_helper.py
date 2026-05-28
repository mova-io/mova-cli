"""Tests for the ipysigma notebook helper (``movate.graph.notebook``).

Covers:

* :func:`load_graph` adapts a mocked graph-API graphology JSON response
  into a NetworkX graph (nodes, edges, attributes, directedness).
* The graphology -> NetworkX adapter (``core.graph.networkx_format``)
  handles the happy path + malformed payloads.
* A friendly install hint + clean ``ImportError`` when the opt-in
  ``graph-notebook`` extra (networkx / ipysigma) is absent.
* :func:`node_detail` fetch shape (path + auth header + parsed dict).

networkx is required to actually build a graph; these tests skip the
networkx-dependent assertions if it isn't installed (it's an opt-in extra)
but still verify the import-guard / install-hint behavior unconditionally.
"""

from __future__ import annotations

import builtins
import importlib

import httpx
import pytest

from movate.core.graph.networkx_format import (
    GraphFormatError,
    graphology_to_networkx,
)
from movate.graph import notebook as nb_helper

_HAS_NETWORKX = importlib.util.find_spec("networkx") is not None

if _HAS_NETWORKX:
    import networkx as nx


# ---------------------------------------------------------------------------
# Fixtures: a small graphology JSON payload
# ---------------------------------------------------------------------------

GRAPHOLOGY_DIRECTED = {
    "attributes": {"name": "kb"},
    "options": {"type": "directed", "multi": False, "allowSelfLoops": True},
    "nodes": [
        {"key": "n1", "attributes": {"label": "Acme", "type": "Org"}},
        {"key": "n2", "attributes": {"label": "Bob", "type": "Person"}},
    ],
    "edges": [
        {
            "key": "e1",
            "source": "n1",
            "target": "n2",
            "attributes": {"weight": 2.0, "type": "EMPLOYS"},
        }
    ],
}


def _mock_transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Adapter: graphology -> networkx
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_NETWORKX, reason="networkx is an opt-in extra")
class TestAdapter:
    def test_adapts_nodes_edges_and_attributes(self) -> None:
        graph = graphology_to_networkx(GRAPHOLOGY_DIRECTED)

        assert isinstance(graph, nx.DiGraph)
        assert set(graph.nodes) == {"n1", "n2"}
        assert graph.nodes["n1"]["label"] == "Acme"
        assert graph.nodes["n1"]["type"] == "Org"
        assert graph.has_edge("n1", "n2")
        assert graph.edges["n1", "n2"]["weight"] == 2.0
        assert graph.edges["n1", "n2"]["type"] == "EMPLOYS"
        # edge key preserved as an attribute
        assert graph.edges["n1", "n2"]["key"] == "e1"
        # top-level graph attributes land on graph.graph
        assert graph.graph["name"] == "kb"

    def test_undirected_default(self) -> None:
        payload = {"nodes": [{"key": "a"}, {"key": "b"}], "edges": []}
        graph = graphology_to_networkx(payload)
        assert isinstance(graph, nx.Graph)
        assert not isinstance(graph, nx.DiGraph)
        assert set(graph.nodes) == {"a", "b"}

    def test_missing_node_key_raises(self) -> None:
        with pytest.raises(GraphFormatError, match="missing required 'key'"):
            graphology_to_networkx({"nodes": [{"attributes": {}}], "edges": []})

    def test_missing_edge_endpoint_raises(self) -> None:
        with pytest.raises(GraphFormatError, match="source"):
            graphology_to_networkx({"nodes": [{"key": "a"}], "edges": [{"source": "a"}]})

    def test_non_dict_payload_raises(self) -> None:
        with pytest.raises(GraphFormatError, match="graphology-JSON object"):
            graphology_to_networkx([])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# load_graph: fetch + adapt against a mocked graph API
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_NETWORKX, reason="networkx is an opt-in extra")
class TestLoadGraph:
    def test_load_graph_adapts_api_response(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json=GRAPHOLOGY_DIRECTED)

        graph = nb_helper.load_graph(
            "prod",
            "my-kb",
            base_url="https://runtime.example/api/v1",
            api_key="mvt_secret",
            transport=_mock_transport(handler),
        )

        assert isinstance(graph, nx.DiGraph)
        assert set(graph.nodes) == {"n1", "n2"}
        # base_url joined to /graph; query params present
        assert "/api/v1/graph" in str(captured["url"])
        assert "target=prod" in str(captured["url"])
        assert "project_id=my-kb" in str(captured["url"])
        # bearer token passed as a header
        assert captured["auth"] == "Bearer mvt_secret"

    def test_load_graph_trailing_slash_base_url(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json={"nodes": [], "edges": []})

        nb_helper.load_graph(
            "dev",
            "p",
            base_url="https://runtime.example/api/v1/",
            api_key="k",
            transport=_mock_transport(handler),
        )
        # no double slash
        assert seen["path"] == "/api/v1/graph"

    def test_load_graph_raises_on_http_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        with pytest.raises(httpx.HTTPStatusError):
            nb_helper.load_graph(
                "prod",
                "p",
                base_url="https://runtime.example",
                api_key="k",
                transport=_mock_transport(handler),
            )


# ---------------------------------------------------------------------------
# node_detail: drill-in fetch
# ---------------------------------------------------------------------------


class TestNodeDetail:
    def test_node_detail_fetch_shape(self) -> None:
        captured: dict[str, str | None] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(
                200,
                json={
                    "id": "n1",
                    "label": "Acme",
                    "properties": {"founded": 1999},
                    "provenance": ["doc-7"],
                },
            )

        detail = nb_helper.node_detail(
            "n1",
            base_url="https://runtime.example/api/v1",
            api_key="mvt_secret",
            transport=_mock_transport(handler),
        )

        assert detail["id"] == "n1"
        assert detail["properties"]["founded"] == 1999
        assert detail["provenance"] == ["doc-7"]
        assert captured["path"] == "/api/v1/graph/nodes/n1"
        assert captured["auth"] == "Bearer mvt_secret"

    def test_node_detail_raises_on_404(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "not found"})

        with pytest.raises(httpx.HTTPStatusError):
            nb_helper.node_detail(
                "nope",
                base_url="https://runtime.example",
                api_key="k",
                transport=_mock_transport(handler),
            )


# ---------------------------------------------------------------------------
# Opt-in extra absent: friendly install hint + clean ImportError
# ---------------------------------------------------------------------------


class TestMissingExtra:
    def test_show_graph_hint_when_ipysigma_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """show_graph raises a friendly ImportError naming the extra when
        ipysigma can't be imported."""
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "ipysigma" or name.startswith("ipysigma."):
                raise ImportError("No module named 'ipysigma'")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(ImportError) as exc:
            nb_helper.show_graph(object())
        assert "graph-notebook" in str(exc.value)
        assert "pip install" in str(exc.value)

    def test_adapter_hint_when_networkx_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The graphology adapter raises a friendly ImportError naming the
        extra when networkx can't be imported."""
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "networkx" or name.startswith("networkx."):
                raise ImportError("No module named 'networkx'")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(ImportError) as exc:
            graphology_to_networkx({"nodes": [], "edges": []})
        assert "graph-notebook" in str(exc.value)
        assert "pip install" in str(exc.value)
