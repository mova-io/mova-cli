"""GraphRAG retrieval — assemble a graph context for an agent prompt.

The dual of :mod:`movate.kb.graph_extract`: where extraction writes the
graph at ingest time, this reads it at query time. The flow mirrors the
existing vector retrieval pipeline but over the entity graph:

1. Embed the query with the same model the graph was built with.
2. **Seed** — vector-search the entities for the closest matches.
3. **Expand** — bounded k-hop walk from those seeds over the relations.
4. **Assemble** — render the reached entities + relations into a compact
   text block the agent can drop into its prompt, grounded with the
   source provenance the graph carries.

Every step is budget-bounded (seed count, hop depth, relation cap) so a
hub entity can't blow up the agent's context window — same discipline as
:mod:`movate.kb.multi_hop`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from movate.core.models import Entity, Relation
from movate.kb.embed import DEFAULT_EMBEDDING_MODEL, embed_texts

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)

# Budget ceilings — clamp caller values so a bad config can't request an
# unbounded traversal. Match the spirit of multi_hop's MAX_HOPS guard.
MAX_SEED_LIMIT = 25
MAX_HOPS = 3
MAX_RELATIONS = 200


@dataclass
class GraphContext:
    """Result of a GraphRAG retrieval: the reached subgraph plus a
    prompt-ready rendering of it."""

    entities: list[Entity]
    relations: list[Relation]
    text: str
    """Rendered context block. Empty string when nothing was retrieved —
    callers can treat falsy ``text`` as "no graph context available"."""

    @property
    def is_empty(self) -> bool:
        return not self.entities


async def graph_retrieve(
    *,
    storage: StorageProvider,
    agent: str,
    tenant_id: str,
    query: str,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
    seed_limit: int = 5,
    hops: int = 1,
    max_relations: int = 50,
) -> GraphContext:
    """Retrieve a graph context for ``query`` from ``agent``'s knowledge graph.

    Args:
        storage: Backend holding the graph.
        agent / tenant_id: Scope.
        query: The user question. Empty / whitespace → empty context.
        embedding_model: MUST match the model the graph was built with —
            the seed search compares query and entity vectors directly.
        api_key: Optional embedding key override.
        seed_limit: How many entities to seed the expansion with. Clamped
            to ``[1, MAX_SEED_LIMIT]``.
        hops: Expansion depth. Clamped to ``[1, MAX_HOPS]``.
        max_relations: Cap on relations in the result. Clamped to
            ``[1, MAX_RELATIONS]``.

    Returns:
        A :class:`GraphContext`. Empty (no entities, ``text=""``) when the
        query is blank or the graph has no match — never raises on an
        empty graph.
    """
    if not query.strip():
        return GraphContext(entities=[], relations=[], text="")

    n_seeds = max(1, min(int(seed_limit), MAX_SEED_LIMIT))
    n_hops = max(1, min(int(hops), MAX_HOPS))
    n_relations = max(1, min(int(max_relations), MAX_RELATIONS))

    query_embedding = (await embed_texts([query], model=embedding_model, api_key=api_key))[0]
    seeds = await storage.search_entities(
        agent=agent,
        tenant_id=tenant_id,
        query_embedding=query_embedding,
        limit=n_seeds,
    )
    if not seeds:
        return GraphContext(entities=[], relations=[], text="")

    subgraph = await storage.expand_neighbors(
        agent=agent,
        tenant_id=tenant_id,
        entity_ids=[s.entity.entity_id for s in seeds],
        hops=n_hops,
        limit=n_relations,
    )
    text = render_graph_context(subgraph.entities, subgraph.relations)
    return GraphContext(entities=subgraph.entities, relations=subgraph.relations, text=text)


def render_graph_context(entities: list[Entity], relations: list[Relation]) -> str:
    """Render entities + relations into a compact, prompt-friendly block.

    Entities first (so relation lines can refer to them by name), then the
    relations as ``src —TYPE→ dst`` lines. Returns ``""`` for an empty
    graph so callers can cheaply skip injecting an empty section.
    """
    if not entities:
        return ""
    id_to_name = {e.entity_id: e.name for e in entities}

    lines: list[str] = ["Entities:"]
    for e in entities:
        suffix = f": {e.description}" if e.description else ""
        lines.append(f"- {e.name} ({e.type}){suffix}")

    if relations:
        lines.append("")
        lines.append("Relationships:")
        for r in relations:
            src = id_to_name.get(r.src_entity_id, r.src_entity_id)
            dst = id_to_name.get(r.dst_entity_id, r.dst_entity_id)
            suffix = f": {r.description}" if r.description else ""
            lines.append(f"- {src} —{r.type}→ {dst}{suffix}")

    return "\n".join(lines)
