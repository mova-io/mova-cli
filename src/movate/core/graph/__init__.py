"""Read-only knowledge-graph query layer (ADR 046).

A thin, backend-agnostic query API over the *already-persisted* GraphRAG
graph (ADR 010): the :class:`~movate.core.models.Entity` / ``Relation``
rows that ``mdk kb ingest --build-graph`` writes through
:meth:`StorageProvider.upsert_entity` / ``upsert_relation``. This package
adds **no persistence and no extraction** — it only *reads* the existing
store through the ``StorageProvider`` Protocol and reshapes the result
into a **graphology-native** JSON document a sigma.js client imports with
zero transform.

Modules:

* :mod:`movate.core.graph.models` — the graphology view models
  (``GraphNode``, ``GraphEdge``, ``GraphologyDoc``) plus ``NodeDetail`` /
  ``Provenance`` for the node-detail endpoint.
* :mod:`movate.core.graph.serialize` — ``to_graphology`` (Entity/Relation
  → graphology doc) with degree-derived ``size`` and type/community-derived
  ``color``.
* :mod:`movate.core.graph.query` — pure windowing / neighbor-expansion /
  search / bounded-traversal compositions over the ``StorageProvider``.

Boundary: depends only on Protocols + core models. Never imports a
concrete backend, the runtime, or the CLI (CLAUDE.md rule 6 — ``cli ⊥
runtime``; ``core`` depends on adapter Protocols).
"""

from __future__ import annotations

from movate.core.graph.models import (
    GraphEdge,
    GraphNode,
    GraphologyDoc,
    NodeDetail,
    NodeSearchHit,
    Provenance,
)
from movate.core.graph.query import (
    DEFAULT_CAP,
    MAX_CAP,
    GraphMode,
    clamp_cap,
    expand_node_neighbors,
    node_detail,
    search_nodes,
    traverse,
    windowed_subgraph,
)
from movate.core.graph.serialize import color_for, size_from_degree, to_graphology

__all__ = [
    "DEFAULT_CAP",
    "MAX_CAP",
    "GraphEdge",
    "GraphMode",
    "GraphNode",
    "GraphologyDoc",
    "NodeDetail",
    "NodeSearchHit",
    "Provenance",
    "clamp_cap",
    "color_for",
    "expand_node_neighbors",
    "node_detail",
    "search_nodes",
    "size_from_degree",
    "to_graphology",
    "traverse",
    "windowed_subgraph",
]
