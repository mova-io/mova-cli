"""Deterministic graph assert — structured facts → graph entities + relations.

Sibling to :mod:`movate.kb.graph_extract`. Where *extraction* DISCOVERS a
probabilistic graph from prose via an LLM, *assertion* WRITES a known, structured
set of facts — a ServiceNow incident and its store/lane/symptom, a dispatched
workflow's outcome, a reboot command id — into the SAME graph, deterministically.
ADR 079.

Like extraction, this module is **pure**: it builds :class:`Entity` /
:class:`Relation` records and never touches storage. Callers persist via
:meth:`StorageProvider.upsert_entity` / ``upsert_relation``, exactly as the
ingest pipeline does for extracted records.

Identity contract (MUST stay byte-compatible with :mod:`graph_extract`):

* ``content_hash = sha256(agent | tenant | norm(name) | norm(type))`` — the
  upsert layer dedups on ``(agent, tenant_id, content_hash)``, so an asserted
  node and an *extracted* node for the same ``(agent, tenant, name, type)``
  collapse to one row. The test suite pins this equivalence
  (``test_graph_assert_ids_match_extract``) so the two builders never drift.
* Unlike extraction's random ``uuid4`` ``entity_id``, an asserted ``entity_id``
  is **derived from the content_hash**, so it is stable across re-asserts and so
  asserted relations reference a deterministic, reproducible endpoint id.

Provenance: every asserted record carries ``metadata = {"source": "assert",
"confidence": 1.0, ...caller}``. ``metadata["confidence"]`` is exactly the field
the ADR 046 ``min_confidence`` floor reads, so asserted facts survive any floor;
``metadata["source"]`` folds into the graphology node properties, giving the UI
an "asserted vs inferred" signal for free (ADR 079 D4).

NOTE on cross-source id reconciliation: when a node was *first* created by
extraction (random ``uuid4`` id) and is *later* asserted, the upsert merges the
row by ``content_hash`` but keeps the original ``entity_id`` — so an asserted
relation built here would reference the (different) deterministic id. Resolving
that is a **persistence-layer** concern (D2/D3: look up the existing id by
content_hash before writing edges); this pure builder is correct and
self-consistent for the asserted set in isolation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from movate.core.models import Entity, Relation
from movate.kb.embed import DEFAULT_EMBEDDING_MODEL, embed_texts, qualified_model_name

# How many entity texts to embed per embedding API call (matches the ingest
# pipeline + graph_extract batch size).
_EMBED_BATCH_SIZE = 64


@dataclass(frozen=True)
class AssertNode:
    """A structured fact to write as one graph node.

    ``type`` + ``name`` form the node's identity (and its dedup hash); reuse the
    exact same pair to merge into / update an existing node. ``metadata`` is
    merged under the builder's ``{"source": "assert", "confidence": 1.0}`` (it
    cannot override those two reserved keys).
    """

    type: str
    name: str
    description: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class AssertEdge:
    """A directed relation between two asserted nodes, referenced by ``name``.

    ``src`` / ``dst`` are node *names* that must appear in the asserted node set
    (mirrors :mod:`graph_extract`'s name→id resolution). Edges whose endpoints
    don't resolve, and self-loops, are dropped.
    """

    src: str
    dst: str
    type: str
    description: str | None = None
    weight: float = 1.0
    metadata: dict[str, Any] | None = None


@dataclass
class _NodeAccum:
    """Mutable accumulator while merging duplicate (name, type) inputs."""

    name: str
    type: str
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _norm(s: str) -> str:
    """Normalize a name/type for dedup: collapse whitespace + lowercase.

    Identical to :func:`graph_extract._norm` — see the identity contract.
    """
    return " ".join(s.strip().lower().split())


def _entity_hash(agent: str, tenant_id: str, name: str, type: str) -> str:
    return hashlib.sha256(f"{agent}|{tenant_id}|{_norm(name)}|{_norm(type)}".encode()).hexdigest()


def _relation_hash(agent: str, tenant_id: str, src_id: str, dst_id: str, type: str) -> str:
    payload = f"{agent}|{tenant_id}|{src_id}|{dst_id}|{_norm(type)}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _clamp_weight(value: Any, *, default: float = 1.0) -> float:
    try:
        w = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, w))


async def build_asserted_graph(
    nodes: Sequence[AssertNode],
    edges: Sequence[AssertEdge] = (),
    *,
    agent: str,
    tenant_id: str,
    project_id: str | None = None,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
) -> tuple[list[Entity], list[Relation]]:
    """Build deterministic, embedded graph records from structured facts.

    Args:
        nodes: The facts to assert as nodes. Duplicate ``(name, type)`` pairs are
            merged (longest description wins; metadata is union-merged). Empty
            input → ``([], [])`` with no embedding calls.
        edges: Directed relations referencing node ``name``s. Dangling and
            self-loop edges are dropped (mirrors extraction).
        agent / tenant_id: Scope stamped on every record and folded into the
            dedup ``content_hash``.
        project_id: Optional ADR 046 D1 project tag; not part of the dedup hash,
            so re-asserting under a project backfills the tag in place.
        embedding_model: Model used to embed node text. MUST match what the KB
            chunks were embedded with so query-time cosine is comparable.
        api_key: Optional embedding key override; otherwise env resolution.

    Returns:
        ``(entities, relations)`` ready to upsert. Entity ``entity_id`` and
        ``content_hash`` are deterministic functions of ``(agent, tenant, name,
        type)``; relations reference those entity ids. Every record carries
        ``metadata["source"] == "assert"`` and ``metadata["confidence"] == 1.0``.
    """
    if not nodes:
        return [], []

    # 1. Dedup nodes by content_hash (collapse repeated (name, type) inputs),
    #    preserving first-seen order for stable embedding batches.
    accums: dict[str, _NodeAccum] = {}
    order: list[str] = []
    for node in nodes:
        name = node.name.strip()
        type_ = node.type.strip()
        if not name or not type_:
            continue
        key = _entity_hash(agent, tenant_id, name, type_)
        accum = accums.get(key)
        if accum is None:
            accum = _NodeAccum(name=name, type=type_)
            accums[key] = accum
            order.append(key)
        desc = (node.description or "").strip()
        if len(desc) > len(accum.description):
            accum.description = desc
        if node.metadata:
            accum.metadata.update(node.metadata)

    if not accums:
        return [], []

    # 2. Embed node text ("name: description", falling back to name).
    ordered = [accums[k] for k in order]
    texts = [a.name if not a.description else f"{a.name}: {a.description}" for a in ordered]
    full_model = qualified_model_name(embedding_model)
    embeddings: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch = texts[i : i + _EMBED_BATCH_SIZE]
        embeddings.extend(await embed_texts(batch, model=embedding_model, api_key=api_key))

    # 3. Build entities with deterministic ids + assert provenance.
    entities: list[Entity] = []
    name_to_id: dict[str, str] = {}
    for accum, emb in zip(ordered, embeddings, strict=True):
        content_hash = _entity_hash(agent, tenant_id, accum.name, accum.type)
        meta = {**accum.metadata, "source": "assert", "confidence": 1.0}
        entities.append(
            Entity(
                entity_id=content_hash,  # deterministic — stable across re-asserts
                tenant_id=tenant_id,
                agent=agent,
                project_id=project_id,
                name=accum.name,
                type=accum.type,
                description=accum.description or None,
                embedding=emb,
                embedding_model=full_model,
                content_hash=content_hash,
                metadata=meta,
            )
        )
        name_to_id.setdefault(_norm(accum.name), content_hash)

    relations = _build_relations(
        edges, name_to_id=name_to_id, agent=agent, tenant_id=tenant_id, project_id=project_id
    )
    return entities, relations


def _build_relations(
    edges: Sequence[AssertEdge],
    *,
    name_to_id: dict[str, str],
    agent: str,
    tenant_id: str,
    project_id: str | None,
) -> list[Relation]:
    merged: dict[str, Relation] = {}
    for edge in edges:
        src_id = name_to_id.get(_norm(edge.src))
        dst_id = name_to_id.get(_norm(edge.dst))
        type_ = edge.type.strip()
        # Drop edges whose endpoints didn't resolve, self-loops, and untyped.
        if not type_ or src_id is None or dst_id is None or src_id == dst_id:
            continue
        content_hash = _relation_hash(agent, tenant_id, src_id, dst_id, type_)
        if content_hash in merged:  # last assertion of an identical edge wins
            continue
        meta = {**(edge.metadata or {}), "source": "assert", "confidence": 1.0}
        merged[content_hash] = Relation(
            relation_id=content_hash,  # deterministic
            tenant_id=tenant_id,
            agent=agent,
            project_id=project_id,
            src_entity_id=src_id,
            dst_entity_id=dst_id,
            type=type_,
            description=(edge.description or "").strip() or None,
            weight=_clamp_weight(edge.weight),
            content_hash=content_hash,
            metadata=meta,
        )
    return list(merged.values())
