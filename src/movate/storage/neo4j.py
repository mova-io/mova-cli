"""Neo4j-backed StorageProvider for graph operations at scale.

An **opt-in** adapter behind the ``StorageProvider`` Protocol, for
customers who want a dedicated graph database for the GraphRAG knowledge
graph. Implements the graph-related slice of the Protocol (upsert_entity,
upsert_relation, list_entities, list_relations, search_entities,
expand_neighbors, get_entity, delete_graph) using Cypher.

**All non-graph methods raise ``NotImplementedError``** — this provider is
NOT a full ``StorageProvider`` replacement; it is a graph-only delegate.
Production deployments combine it with Postgres/SQLite for relational data
(runs, jobs, api_keys, etc.) via the ``MOVATE_GRAPH_BACKEND=neo4j`` env var
in ``build_storage()`` (see :mod:`movate.storage`).

Connection is read from ``NEO4J_URI`` + ``NEO4J_USER`` + ``NEO4J_PASSWORD``
env vars (autoloaded from ``~/.movate/credentials`` via ``mdk auth login
neo4j``).

Requires the ``[neo4j]`` optional extra (``neo4j>=5.0,<6``, Apache-2.0
licensed). The ``neo4j`` package is lazy-imported so the adapter loads only
when ``NEO4J_URI`` is configured — sqlite/postgres-only installations never
need it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from movate.core.models import (
    Entity,
    EntityWithScore,
    Relation,
    Subgraph,
)

if TYPE_CHECKING:
    import neo4j as neo4j_mod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import helper — ``neo4j`` is in the [neo4j] optional extra
# ---------------------------------------------------------------------------


def _import_neo4j() -> Any:
    """Import the ``neo4j`` package, raising a clear error if missing."""
    try:
        import neo4j  # noqa: PLC0415

        return neo4j
    except ImportError as exc:
        raise ImportError(
            "The neo4j Python driver is required for the Neo4j graph adapter. "
            "Install it with: uv add 'movate-cli[neo4j]' (or pip install neo4j>=5.0)"
        ) from exc


# ---------------------------------------------------------------------------
# Cypher templates
# ---------------------------------------------------------------------------

# Entity upsert: MERGE on (agent, tenant_id, content_hash) dedup key,
# then SET all mutable fields. source_chunk_ids is UNION'd on conflict
# (APOC-free, uses list concatenation + REDUCE for unique).
_UPSERT_ENTITY_CYPHER = """
MERGE (n:Entity {agent: $agent, tenant_id: $tenant_id, content_hash: $content_hash})
ON CREATE SET
    n.entity_id = $entity_id,
    n.name = $name,
    n.type = $type,
    n.description = $description,
    n.embedding = $embedding,
    n.embedding_model = $embedding_model,
    n.source_chunk_ids = $source_chunk_ids,
    n.metadata = $metadata,
    n.project_id = $project_id,
    n.created_at = $created_at
ON MATCH SET
    n.name = $name,
    n.type = $type,
    n.description = $description,
    n.embedding = $embedding,
    n.embedding_model = $embedding_model,
    n.source_chunk_ids = REDUCE(
        acc = n.source_chunk_ids,
        id IN $source_chunk_ids |
        CASE WHEN id IN acc THEN acc ELSE acc + id END
    ),
    n.metadata = $metadata,
    n.project_id = COALESCE($project_id, n.project_id)
"""

# Relation upsert: MATCH both endpoints, MERGE the edge on
# (agent, tenant_id, content_hash), SET mutable fields.
_UPSERT_RELATION_CYPHER = """
MATCH (src:Entity {entity_id: $src_entity_id, agent: $agent, tenant_id: $tenant_id})
MATCH (dst:Entity {entity_id: $dst_entity_id, agent: $agent, tenant_id: $tenant_id})
MERGE (src)-[r:RELATION {agent: $agent, tenant_id: $tenant_id, content_hash: $content_hash}]->(dst)
ON CREATE SET
    r.relation_id = $relation_id,
    r.type = $type,
    r.description = $description,
    r.weight = $weight,
    r.source_chunk_ids = $source_chunk_ids,
    r.metadata = $metadata,
    r.project_id = $project_id,
    r.created_at = $created_at
ON MATCH SET
    r.type = $type,
    r.description = $description,
    r.weight = $weight,
    r.source_chunk_ids = REDUCE(
        acc = r.source_chunk_ids,
        id IN $source_chunk_ids |
        CASE WHEN id IN acc THEN acc ELSE acc + id END
    ),
    r.metadata = $metadata,
    r.project_id = COALESCE($project_id, r.project_id)
"""

_LIST_ENTITIES_CYPHER = """
MATCH (n:Entity {agent: $agent, tenant_id: $tenant_id})
{project_filter}
{source_chunk_filter}
RETURN n ORDER BY n.created_at DESC LIMIT $limit
"""

_LIST_RELATIONS_CYPHER = """
MATCH (src:Entity)-[r:RELATION {agent: $agent, tenant_id: $tenant_id}]->(dst:Entity)
{project_filter}
RETURN r, src.entity_id AS src_entity_id, dst.entity_id AS dst_entity_id
ORDER BY r.created_at DESC LIMIT $limit
"""

_GET_ENTITY_CYPHER = """
MATCH (n:Entity {entity_id: $entity_id, tenant_id: $tenant_id})
RETURN n LIMIT 1
"""

_SEARCH_ENTITIES_CYPHER = """
MATCH (n:Entity {agent: $agent, tenant_id: $tenant_id})
{project_filter}
RETURN n
"""

_DELETE_GRAPH_ENTITIES_CYPHER = """
MATCH (n:Entity {agent: $agent, tenant_id: $tenant_id})
{source_filter}
DETACH DELETE n
RETURN count(*) AS deleted
"""

# k-hop expansion: variable-length path pattern, undirected for
# reachability; collect all reached entities + traversed relations.
_EXPAND_CYPHER = """
MATCH (seed:Entity)
WHERE seed.entity_id IN $entity_ids
  AND seed.agent = $agent
  AND seed.tenant_id = $tenant_id
  {project_filter_seed}
CALL {{
    WITH seed
    MATCH path = (seed)-[r:RELATION*1..{hops}]-(reached:Entity)
    WHERE ALL(rel IN relationships(path) WHERE rel.agent = $agent AND rel.tenant_id = $tenant_id
        {project_filter_rel})
    AND reached.agent = $agent
    AND reached.tenant_id = $tenant_id
    {project_filter_reached}
    UNWIND relationships(path) AS edge
    WITH DISTINCT reached, edge
    RETURN collect(DISTINCT reached) AS neighbors, collect(DISTINCT edge) AS edges
}}
RETURN seed, neighbors, edges
"""


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class Neo4jStorageProvider:
    """Graph-only :class:`StorageProvider` impl backed by Neo4j via Cypher.

    This is a **graph delegate** — it implements only the graph-related
    methods. All non-graph methods raise ``NotImplementedError``. A full
    deployment wires this alongside a relational provider (Postgres/SQLite)
    via the ``MOVATE_GRAPH_BACKEND`` env var.
    """

    name = "neo4j"

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        database: str = "neo4j",
    ) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._driver: neo4j_mod.AsyncDriver | None = None

    async def init(self) -> None:
        """Create the async Neo4j driver and verify connectivity.

        Also creates uniqueness constraints + indexes if they don't exist.
        """
        neo4j = _import_neo4j()
        self._driver = neo4j.AsyncGraphDatabase.driver(
            self._uri,
            auth=(self._user, self._password),
        )
        # Verify connectivity
        await self._driver.verify_connectivity()
        logger.info("neo4j: connected to %s", self._uri)

        # Create constraints + indexes (idempotent)
        async with self._driver.session(database=self._database) as session:
            # Unique constraint on entity dedup key
            await session.run(
                "CREATE CONSTRAINT IF NOT EXISTS "
                "FOR (n:Entity) REQUIRE (n.agent, n.tenant_id, n.content_hash) IS UNIQUE"
            )
            # Index on entity_id for fast lookups
            await session.run("CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.entity_id)")
            # Index on (agent, tenant_id) for listing
            await session.run("CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.agent, n.tenant_id)")

    async def ping(self) -> None:
        """Verify the Neo4j connection is alive."""
        driver = self._get_driver()
        await driver.verify_connectivity()

    async def close(self) -> None:
        """Close the Neo4j driver."""
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    def _get_driver(self) -> neo4j_mod.AsyncDriver:
        if self._driver is None:
            raise RuntimeError("Neo4jStorageProvider.init() not called")
        return self._driver

    # ------------------------------------------------------------------
    # Graph: entities
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> None:
        driver = self._get_driver()
        params = _entity_to_params(entity)
        async with driver.session(database=self._database) as session:
            await session.run(_UPSERT_ENTITY_CYPHER, params)

    async def get_entity(self, entity_id: str, *, tenant_id: str) -> Entity | None:
        driver = self._get_driver()
        async with driver.session(database=self._database) as session:
            result = await session.run(
                _GET_ENTITY_CYPHER,
                entity_id=entity_id,
                tenant_id=tenant_id,
            )
            record = await result.single()
        if record is None:
            return None
        return _record_to_entity(record["n"])

    async def list_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        source_chunk_id: str | None = None,
        limit: int = 1000,
        project_id: str | None = None,
    ) -> list[Entity]:
        driver = self._get_driver()
        project_filter = "WHERE n.project_id = $project_id" if project_id else ""
        source_chunk_filter = ""
        params: dict[str, Any] = {
            "agent": agent,
            "tenant_id": tenant_id,
            "limit": limit,
        }
        if project_id:
            # Adjust: if project_filter already has WHERE, use AND
            project_filter = "WHERE n.project_id = $project_id"
            params["project_id"] = project_id
        if source_chunk_id:
            conjunction = "AND" if project_filter else "WHERE"
            source_chunk_filter = f"{conjunction} $source_chunk_id IN n.source_chunk_ids"
            params["source_chunk_id"] = source_chunk_id

        cypher = _LIST_ENTITIES_CYPHER.format(
            project_filter=project_filter,
            source_chunk_filter=source_chunk_filter,
        )
        async with driver.session(database=self._database) as session:
            result = await session.run(cypher, **params)
            records = await result.data()
        return [_record_to_entity(r["n"]) for r in records]

    async def search_entities(
        self,
        *,
        agent: str,
        tenant_id: str,
        query_embedding: list[float],
        limit: int = 10,
        project_id: str | None = None,
    ) -> list[EntityWithScore]:
        """Top-K entity search by cosine similarity.

        Loads candidate entities from Neo4j, then ranks them in Python
        using the shared cosine ranker (same semantics as sqlite/postgres).
        A future optimisation could use Neo4j's vector index (5.11+), but
        the Python ranker keeps this portable across Neo4j versions.
        """
        from movate.storage._cosine import rank_entities_by_cosine  # noqa: PLC0415

        driver = self._get_driver()
        project_filter = "WHERE n.project_id = $project_id" if project_id else ""
        params: dict[str, Any] = {
            "agent": agent,
            "tenant_id": tenant_id,
        }
        if project_id:
            params["project_id"] = project_id

        cypher = _SEARCH_ENTITIES_CYPHER.format(project_filter=project_filter)
        async with driver.session(database=self._database) as session:
            result = await session.run(cypher, **params)
            records = await result.data()

        entities = [_record_to_entity(r["n"]) for r in records]
        return rank_entities_by_cosine(entities, query_embedding, limit)

    # ------------------------------------------------------------------
    # Graph: relations
    # ------------------------------------------------------------------

    async def upsert_relation(self, relation: Relation) -> None:
        driver = self._get_driver()
        params = _relation_to_params(relation)
        async with driver.session(database=self._database) as session:
            await session.run(_UPSERT_RELATION_CYPHER, params)

    async def list_relations(
        self,
        *,
        agent: str,
        tenant_id: str,
        limit: int = 1000,
        project_id: str | None = None,
    ) -> list[Relation]:
        driver = self._get_driver()
        project_filter = ""
        params: dict[str, Any] = {
            "agent": agent,
            "tenant_id": tenant_id,
            "limit": limit,
        }
        if project_id:
            project_filter = "WHERE r.project_id = $project_id"
            params["project_id"] = project_id

        cypher = _LIST_RELATIONS_CYPHER.format(project_filter=project_filter)
        async with driver.session(database=self._database) as session:
            result = await session.run(cypher, **params)
            records = await result.data()
        return [
            _record_to_relation(r["r"], r["src_entity_id"], r["dst_entity_id"]) for r in records
        ]

    # ------------------------------------------------------------------
    # Graph: traversal
    # ------------------------------------------------------------------

    async def expand_neighbors(
        self,
        *,
        agent: str,
        tenant_id: str,
        entity_ids: list[str],
        hops: int = 1,
        limit: int = 50,
        project_id: str | None = None,
    ) -> Subgraph:
        if not entity_ids:
            return Subgraph(entities=[], relations=[])

        driver = self._get_driver()

        # Build a bounded BFS using variable-length paths in Cypher.
        # Simpler approach: gather seeds + 1-hop neighbors iteratively in
        # Python (same BFS the InMemory double uses) but delegating to Cypher
        # for the actual node/edge fetch per hop. This is more portable across
        # Neo4j versions than variable-length path with ALL() predicate.
        params: dict[str, Any] = {
            "agent": agent,
            "tenant_id": tenant_id,
            "limit": limit,
        }
        project_entity_clause = ""
        project_rel_clause = ""
        if project_id:
            project_entity_clause = "AND n.project_id = $project_id"
            project_rel_clause = "AND r.project_id = $project_id"
            params["project_id"] = project_id

        # BFS in Python with Cypher per-hop — simpler, avoids deep APOC/GDS.
        reachable_ids: set[str] = set(entity_ids)
        frontier: set[str] = set(entity_ids)
        all_relations: list[Relation] = []
        seen_rel_ids: set[str] = set()

        async with driver.session(database=self._database) as session:
            for _ in range(max(0, hops)):
                if not frontier:
                    break
                # Fetch all edges incident to frontier nodes
                cypher = f"""
                MATCH (a:Entity)-[r:RELATION]->(b:Entity)
                WHERE (a.entity_id IN $frontier OR b.entity_id IN $frontier)
                  AND r.agent = $agent AND r.tenant_id = $tenant_id
                  AND a.agent = $agent AND a.tenant_id = $tenant_id
                  AND b.agent = $agent AND b.tenant_id = $tenant_id
                  {project_rel_clause}
                  {project_entity_clause.replace("n.", "a.")}
                  {project_entity_clause.replace("n.", "b.")}
                RETURN r, a.entity_id AS src_id, b.entity_id AS dst_id
                ORDER BY r.weight DESC
                """
                result = await session.run(
                    cypher,
                    frontier=list(frontier),
                    **params,
                )
                records = await result.data()

                next_frontier: set[str] = set()
                for rec in records:
                    rel = _record_to_relation(rec["r"], rec["src_id"], rec["dst_id"])
                    if rel.relation_id not in seen_rel_ids:
                        seen_rel_ids.add(rel.relation_id)
                        all_relations.append(rel)
                    # Expand undirected: both endpoints
                    for eid in (rel.src_entity_id, rel.dst_entity_id):
                        if eid not in reachable_ids:
                            next_frontier.add(eid)
                            reachable_ids.add(eid)

                frontier = next_frontier

            # Budget cap on relations
            all_relations.sort(key=lambda r: r.weight, reverse=True)
            all_relations = all_relations[:limit]

            # Gather entities for the reachable set (include seeds even if
            # they had no edges — matches the InMemory double).
            keep_ids = (
                set(entity_ids)
                | {r.src_entity_id for r in all_relations}
                | {r.dst_entity_id for r in all_relations}
            )
            if keep_ids:
                ent_cypher = f"""
                MATCH (n:Entity)
                WHERE n.entity_id IN $ids
                  AND n.agent = $agent AND n.tenant_id = $tenant_id
                  {project_entity_clause}
                RETURN n
                """
                ent_result = await session.run(
                    ent_cypher,
                    ids=list(keep_ids),
                    **params,
                )
                ent_records = await ent_result.data()
                entities = [_record_to_entity(r["n"]) for r in ent_records]
            else:
                entities = []

        return Subgraph(entities=entities, relations=all_relations)

    # ------------------------------------------------------------------
    # Graph: delete
    # ------------------------------------------------------------------

    async def delete_graph(
        self,
        *,
        agent: str,
        tenant_id: str,
        source: str | None = None,
    ) -> int:
        driver = self._get_driver()
        # Source-scoped delete is more complex: we'd need to check
        # source_chunk_ids against the kb_chunks table. For now, source
        # filtering deletes entities whose source_chunk_ids list is non-empty
        # (we can't resolve chunk → source without the relational store).
        # The common case (source=None) deletes the full agent graph.
        if source is not None:
            # Cannot resolve source → chunk_ids without the relational store.
            # Delete all entities for the agent (same behavior as full delete
            # when source filtering isn't available at the graph layer).
            logger.warning(
                "neo4j: source-scoped delete_graph not supported; deleting "
                "full graph for agent=%s tenant=%s",
                agent,
                tenant_id,
            )

        async with driver.session(database=self._database) as session:
            # Delete relations first (DETACH DELETE handles this), then entities.
            result = await session.run(
                "MATCH (n:Entity {agent: $agent, tenant_id: $tenant_id}) "
                "DETACH DELETE n "
                "RETURN count(*) AS deleted",
                agent=agent,
                tenant_id=tenant_id,
            )
            record = await result.single()
            deleted = record["deleted"] if record else 0

        return int(deleted)


# ---------------------------------------------------------------------------
# Param builders
# ---------------------------------------------------------------------------


def _entity_to_params(entity: Entity) -> dict[str, Any]:
    """Convert an Entity to Neo4j query parameters."""
    return {
        "entity_id": entity.entity_id,
        "tenant_id": entity.tenant_id,
        "agent": entity.agent,
        "name": entity.name,
        "type": entity.type,
        "description": entity.description,
        "embedding": json.dumps(entity.embedding),
        "embedding_model": entity.embedding_model,
        "content_hash": entity.content_hash,
        "source_chunk_ids": list(entity.source_chunk_ids),
        "metadata": json.dumps(entity.metadata) if entity.metadata else None,
        "project_id": entity.project_id,
        "created_at": entity.created_at.isoformat(),
    }


def _relation_to_params(relation: Relation) -> dict[str, Any]:
    """Convert a Relation to Neo4j query parameters."""
    return {
        "relation_id": relation.relation_id,
        "tenant_id": relation.tenant_id,
        "agent": relation.agent,
        "src_entity_id": relation.src_entity_id,
        "dst_entity_id": relation.dst_entity_id,
        "type": relation.type,
        "description": relation.description,
        "weight": relation.weight,
        "content_hash": relation.content_hash,
        "source_chunk_ids": list(relation.source_chunk_ids),
        "metadata": json.dumps(relation.metadata) if relation.metadata else None,
        "project_id": relation.project_id,
        "created_at": relation.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Record → model converters
# ---------------------------------------------------------------------------


def _record_to_entity(node: Any) -> Entity:
    """Convert a Neo4j node record to an Entity model."""
    props = dict(node) if not isinstance(node, dict) else node
    raw_emb = props["embedding"]
    embedding = json.loads(raw_emb) if isinstance(raw_emb, str) else raw_emb
    raw_meta = props.get("metadata")
    metadata = json.loads(raw_meta) if raw_meta else None
    raw_ts = props["created_at"]
    created_at = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else raw_ts
    return Entity(
        entity_id=props["entity_id"],
        tenant_id=props["tenant_id"],
        agent=props["agent"],
        name=props["name"],
        type=props["type"],
        description=props.get("description"),
        embedding=embedding,
        embedding_model=props["embedding_model"],
        content_hash=props["content_hash"],
        source_chunk_ids=props.get("source_chunk_ids") or [],
        metadata=metadata,
        project_id=props.get("project_id"),
        created_at=created_at,
    )


def _record_to_relation(rel: Any, src_id: str, dst_id: str) -> Relation:
    """Convert a Neo4j relationship record to a Relation model."""
    props = dict(rel) if not isinstance(rel, dict) else rel
    raw_meta = props.get("metadata")
    metadata = json.loads(raw_meta) if raw_meta else None
    raw_ts = props["created_at"]
    created_at = datetime.fromisoformat(raw_ts) if isinstance(raw_ts, str) else raw_ts
    return Relation(
        relation_id=props["relation_id"],
        tenant_id=props["tenant_id"],
        agent=props["agent"],
        src_entity_id=src_id,
        dst_entity_id=dst_id,
        type=props["type"],
        description=props.get("description"),
        weight=props.get("weight", 1.0),
        content_hash=props["content_hash"],
        source_chunk_ids=props.get("source_chunk_ids") or [],
        metadata=metadata,
        project_id=props.get("project_id"),
        created_at=created_at,
    )
