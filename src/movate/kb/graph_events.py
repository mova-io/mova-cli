"""Project graph-mutation deltas onto the events outbox (ADR 046 D6 — the
knowledge-graph growth stream).

When a KB ingest extracts + persists a node/edge (``kb.ingest`` →
``storage.upsert_entity`` / ``upsert_relation``), we want the sigma viewer
to **animate the graph growing in real time**. ADR 046 D6 specifies this
as a *typed projection of the ADR 035 D3 SSE stream*, not a new transport:
each upserted node/edge becomes one ``graph.node.added`` /
``graph.edge.added`` :class:`~movate.core.events.Event` on the durable
events outbox, and the graph-growth SSE endpoint live-tails the outbox
(filtered to those kinds + the agent/project scope) exactly the way the
ADR 035 events stream tails it.

Why the **durable outbox** (not an in-process bus): the graph is built by
``mdk kb ingest --build-graph`` (the CLI), while the viewer is served by a
*separate* runtime process. An in-memory pub/sub would never cross that
boundary. The outbox is shared storage, so a node the CLI writes in one
process is streamed by the runtime in another — and a dropped SSE
connection reconciles by re-fetching the snapshot (ADR 046 failure modes).

Boundary discipline (CLAUDE.md rule 6): this is a ``kb``-layer helper that
uses only the ``core`` event/graph models + the ``StorageProvider``
Protocol (``record_event``) — exactly the seams ``kb`` already depends on.
It imports **no** runtime and **no** concrete backend, so both the CLI
ingest path and a future runtime ingest path can wire it without a
cross-plane import.

Failure isolation: the durable upsert has already committed before the
event is recorded, so a ``record_event`` hiccup is pure observability
noise. The publisher swallows + logs it (and ``kb.ingest`` wraps the hook
again) — a flaky outbox never breaks ingest (CLAUDE.md rule 10).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from movate.core.events import Event, EventKind
from movate.core.graph.models import GraphEdge, GraphologyDoc
from movate.core.graph.serialize import to_graphology
from movate.core.models import Entity, Relation

if TYPE_CHECKING:
    from movate.kb.ingest import GraphMutationFn
    from movate.storage.base import StorageProvider

log = logging.getLogger(__name__)


def _node_fragment(entity: Entity) -> GraphologyDoc:
    """A one-node graphology fragment for ``entity``.

    Reuses :func:`to_graphology` (single entity, no relations) so a live
    ``node.added`` frame carries the exact same node shape — ``label`` /
    ``type`` / degree-seeded ``size`` / ``color`` (+ ``community`` /
    ``x`` / ``y`` when stored) — the windowed ``GET .../graph`` endpoint
    emits. The client merges it with the same zero-transform
    ``graph.import(...)`` it uses everywhere else."""
    return to_graphology([entity], [])


def _edge_fragment(relation: Relation) -> GraphologyDoc:
    """A one-edge graphology fragment for ``relation``.

    The serializer drops an edge whose endpoints aren't both in the same
    window, so we build the single edge directly here. The endpoint nodes
    arrive as their own ``node.added`` frames (extraction upserts entities
    before relations); the viewer's import drops an edge whose endpoints
    aren't present yet, so out-of-order or partial windows never raise —
    the edge simply lands once both ends exist."""
    edge = GraphEdge(
        key=relation.relation_id,
        source=relation.src_entity_id,
        target=relation.dst_entity_id,
        attributes={"label": relation.type, "weight": relation.weight},
    )
    return GraphologyDoc(attributes={}, nodes=[], edges=[edge])


def make_outbox_publisher(
    storage: StorageProvider,
    *,
    tenant_id: str,
    agent: str,
) -> GraphMutationFn:
    """Build the ``on_graph_mutation`` hook the KB ingest path fires.

    Returns an async callback ``(record, project_id) -> None`` that records
    one ADR 035 outbox event per upserted node/edge:

    * an :class:`~movate.core.models.Entity` → ``graph.node.added`` whose
      ``data`` is a one-node graphology fragment + ``project_id``,
    * a :class:`~movate.core.models.Relation` → ``graph.edge.added`` whose
      ``data`` is a one-edge graphology fragment + ``project_id``.

    ``subject`` is the agent name (the graph's owner — what the growth
    stream filters on) and ``tenant_id`` scopes the event so no
    cross-tenant delta can ever reach another tenant's stream. The record
    is awaited inline (cheap insert; ingest is already a batch job) and any
    failure is swallowed + logged so a flaky outbox never breaks ingest."""

    async def _publish(record: Entity | Relation, project_id: str | None) -> None:
        if isinstance(record, Entity):
            kind = EventKind.GRAPH_NODE_ADDED
            fragment = _node_fragment(record)
        else:
            kind = EventKind.GRAPH_EDGE_ADDED
            fragment = _edge_fragment(record)
        data = fragment.model_dump(mode="json")
        # ``project_id`` rides alongside the fragment (not inside the
        # graphology doc, which stays import-clean) so the growth stream
        # can filter ``?project=`` without re-reading storage.
        data["project_id"] = project_id
        event = Event(tenant_id=tenant_id, kind=kind.value, subject=agent, data=data)
        try:
            await storage.record_event(event)
        except Exception:
            # The durable upsert already committed — a record_event flake
            # is observability noise, not a correctness failure. Swallow +
            # log; ingest continues (CLAUDE.md rule 10).
            log.warning(
                "graph_growth_event_record_failed kind=%s subject=%s — ingest unaffected",
                kind.value,
                agent,
                exc_info=True,
            )

    return _publish


__all__ = ["make_outbox_publisher"]
