"""GraphRAG extraction — KB chunks → knowledge-graph entities + relations.

Given a batch of :class:`~movate.core.models.KbChunk` (the output of the
ingest pipeline), an LLM extracts the salient entities and the relations
between them from each chunk's text. Entities are deduped/merged across
chunks (so the same concept appearing in three passages becomes one node
with three source citations), embedded for vector-seed retrieval, and
returned alongside the resolved relations.

The result feeds :meth:`StorageProvider.upsert_entity` /
``upsert_relation`` — this module only builds records, it never touches
storage, which keeps it pure and trivially testable.

Design mirrors :mod:`movate.kb.multi_hop`: the LLM call is injected as a
``complete_fn`` so tests run without network access, and every per-chunk
failure is tolerated (logged + skipped) rather than aborting the whole
graph build — a partial graph is more useful than none.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from movate.core.models import Entity, KbChunk, Relation
from movate.kb.embed import DEFAULT_EMBEDDING_MODEL, embed_texts, qualified_model_name

logger = logging.getLogger(__name__)

# Same default tier as the rewriter / reranker / multi-hop planner — a
# cheap, fast model is the right call for span-level extraction.
DEFAULT_EXTRACTION_MODEL = "anthropic/claude-haiku-4-5-20251001"

# How many entity texts to embed per embedding API call (matches the
# ingest pipeline's batch size).
_EMBED_BATCH_SIZE = 64

# Defensive per-chunk prompt cap. Chunks are ~500 tokens by default, so
# this only bites on a pathologically large chunk.
_MAX_CHUNK_CHARS = 6000

# A callable that takes a prompt and returns the LLM's raw text response.
# Injected so tests can supply canned JSON without a live model.
CompleteFn = Callable[[str], Awaitable[str]]

_EXTRACTION_PROMPT = """\
You extract a knowledge graph from a single passage of a document.

From the PASSAGE below, identify:
1. ENTITIES — the salient, reusable things the passage is about: concepts,
   products, features, policies, plans/tiers, systems, organizations,
   roles. Skip generic filler words.
2. RELATIONS — directed connections between those entities that the
   passage explicitly states or clearly implies.

Rules:
- Use ONLY information in the passage. Do not add outside knowledge.
- Give each entity a canonical `name` (how you'd refer to it generally,
  not a pronoun) and a short `type` label (e.g. "Feature", "Policy",
  "Tier", "System", "Organization").
- Relation `src` and `dst` MUST be entity names you listed in `entities`.
- Relation `type` is an UPPER_SNAKE predicate (e.g. REQUIRES, GOVERNS,
  PART_OF, SUPERSEDES, APPLIES_TO).
- `weight` is your confidence in the relation, 0.0 to 1.0.
- If the passage has no meaningful entities, return empty lists.

Return ONLY a JSON object, no prose, in exactly this shape:
{{"entities": [{{"name": "...", "type": "...", "description": "..."}}],
  "relations": [{{"src": "...", "dst": "...", "type": "...",
                  "description": "...", "weight": 0.0}}]}}

PASSAGE:
{passage}
"""


@dataclass
class _EntityAccum:
    """Mutable accumulator while merging an entity across chunks."""

    entity_id: str
    name: str
    type: str
    description: str = ""
    source_chunk_ids: set[str] = field(default_factory=set)


@dataclass
class _RelationAccum:
    src_entity_id: str
    dst_entity_id: str
    type: str
    description: str = ""
    weight: float = 0.0
    source_chunk_ids: set[str] = field(default_factory=set)


def _norm(s: str) -> str:
    """Normalize a name/type for dedup: collapse whitespace + lowercase."""
    return " ".join(s.strip().lower().split())


def _entity_hash(agent: str, tenant_id: str, name: str, type: str) -> str:
    return hashlib.sha256(f"{agent}|{tenant_id}|{_norm(name)}|{_norm(type)}".encode()).hexdigest()


def _relation_hash(agent: str, tenant_id: str, src_id: str, dst_id: str, type: str) -> str:
    payload = f"{agent}|{tenant_id}|{src_id}|{dst_id}|{_norm(type)}"
    return hashlib.sha256(payload.encode()).hexdigest()


async def extract_graph(
    chunks: list[KbChunk],
    *,
    agent: str,
    tenant_id: str,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    model: str = DEFAULT_EXTRACTION_MODEL,
    api_key: str | None = None,
    timeout_s: float = 30.0,
    complete_fn: CompleteFn | None = None,
    project_id: str | None = None,
) -> tuple[list[Entity], list[Relation]]:
    """Extract a merged, embedded knowledge graph from ``chunks``.

    Args:
        chunks: KB chunks to extract from. Their ``chunk_id``s become the
            ``source_chunk_ids`` provenance on every produced record.
            Empty input → ``([], [])`` with no API calls.
        agent / tenant_id: Scope stamped on every entity + relation, and
            folded into the dedup ``content_hash``.
        project_id: Optional project (ADR 040/046 D1) stamped on every
            produced node/edge for project-grain scoping of the graph
            viewer. ``None`` (default) leaves the column null — NOT part of
            the dedup ``content_hash``, so re-ingesting under a project
            backfills the tag in place. Additive + backward-compatible.
        embedding_model: Model used to embed entity text for vector seed.
            MUST match what the KB chunks were embedded with so query-time
            cosine is comparable.
        model: LLM used for extraction (LiteLLM-format identifier).
        api_key: Optional override; otherwise LiteLLM env resolution.
        timeout_s: Per-chunk LLM timeout.
        complete_fn: Injected LLM caller (prompt → raw text). Defaults to a
            LiteLLM-backed call. Tests pass a stub returning canned JSON.

    Returns:
        ``(entities, relations)`` ready to upsert. Entities are deduped by
        normalized (name, type); relations by (resolved endpoints, type),
        each with provenance unioned across the chunks they came from.
        Relations whose endpoints didn't resolve to an extracted entity
        are dropped. Never raises — a failing chunk is skipped.
    """
    if not chunks:
        return [], []

    call = complete_fn or _default_complete_fn(model=model, api_key=api_key, timeout_s=timeout_s)

    entities: dict[str, _EntityAccum] = {}  # content_hash -> accum
    # Per-chunk raw relations, deferred until all entities are known so
    # name → entity_id resolution sees the full set.
    raw_relations: list[tuple[dict[str, Any], str]] = []  # (relation dict, chunk_id)

    for chunk in chunks:
        passage = chunk.text.strip()
        if not passage:
            continue
        if len(passage) > _MAX_CHUNK_CHARS:
            passage = passage[:_MAX_CHUNK_CHARS]
        try:
            raw = await call(_EXTRACTION_PROMPT.format(passage=passage))
        except Exception as exc:  # best-effort: a bad chunk shouldn't abort the build
            logger.warning("graph extraction LLM call failed for chunk %s: %s", chunk.chunk_id, exc)
            continue
        parsed = _parse_extraction(raw)
        if parsed is None:
            logger.warning("graph extraction output unparseable for chunk %s", chunk.chunk_id)
            continue
        for ent in parsed.get("entities", []):
            _accumulate_entity(
                entities, ent, agent=agent, tenant_id=tenant_id, chunk_id=chunk.chunk_id
            )
        for rel in parsed.get("relations", []):
            if isinstance(rel, dict):
                raw_relations.append((rel, chunk.chunk_id))

    if not entities:
        return [], []

    # Resolve relation endpoints by normalized name. On a name collision
    # across types, the first-registered entity wins (an MVP simplification
    # — the common case is one entity per name within a single agent's KB).
    name_to_id: dict[str, str] = {}
    for accum in entities.values():
        name_to_id.setdefault(_norm(accum.name), accum.entity_id)

    relations = _merge_relations(
        raw_relations,
        name_to_id=name_to_id,
        agent=agent,
        tenant_id=tenant_id,
        project_id=project_id,
    )

    built_entities = await _embed_entities(
        list(entities.values()),
        agent=agent,
        tenant_id=tenant_id,
        embedding_model=embedding_model,
        api_key=api_key,
        project_id=project_id,
    )
    return built_entities, relations


def _accumulate_entity(
    entities: dict[str, _EntityAccum],
    ent: Any,
    *,
    agent: str,
    tenant_id: str,
    chunk_id: str,
) -> None:
    if not isinstance(ent, dict):
        return
    name = str(ent.get("name", "")).strip()
    type_ = str(ent.get("type", "")).strip()
    if not name or not type_:
        return
    description = str(ent.get("description", "")).strip()
    key = _entity_hash(agent, tenant_id, name, type_)
    accum = entities.get(key)
    if accum is None:
        accum = _EntityAccum(entity_id=uuid4().hex, name=name, type=type_)
        entities[key] = accum
    # Keep the longest description seen (the most informative summary).
    if len(description) > len(accum.description):
        accum.description = description
    accum.source_chunk_ids.add(chunk_id)


def _merge_relations(
    raw_relations: list[tuple[dict[str, Any], str]],
    *,
    name_to_id: dict[str, str],
    agent: str,
    tenant_id: str,
    project_id: str | None = None,
) -> list[Relation]:
    merged: dict[str, _RelationAccum] = {}
    for rel, chunk_id in raw_relations:
        src_name = str(rel.get("src", "")).strip()
        dst_name = str(rel.get("dst", "")).strip()
        type_ = str(rel.get("type", "")).strip()
        if not src_name or not dst_name or not type_:
            continue
        src_id = name_to_id.get(_norm(src_name))
        dst_id = name_to_id.get(_norm(dst_name))
        # Drop edges whose endpoints didn't resolve to an extracted entity,
        # and self-loops (no traversal value).
        if src_id is None or dst_id is None or src_id == dst_id:
            continue
        key = _relation_hash(agent, tenant_id, src_id, dst_id, type_)
        weight = _clamp_weight(rel.get("weight"))
        description = str(rel.get("description", "")).strip()
        accum = merged.get(key)
        if accum is None:
            accum = _RelationAccum(
                src_entity_id=src_id, dst_entity_id=dst_id, type=type_, weight=weight
            )
            merged[key] = accum
        accum.weight = max(accum.weight, weight)
        if len(description) > len(accum.description):
            accum.description = description
        accum.source_chunk_ids.add(chunk_id)

    return [
        Relation(
            tenant_id=tenant_id,
            agent=agent,
            project_id=project_id,
            src_entity_id=a.src_entity_id,
            dst_entity_id=a.dst_entity_id,
            type=a.type,
            description=a.description or None,
            weight=a.weight,
            content_hash=_relation_hash(agent, tenant_id, a.src_entity_id, a.dst_entity_id, a.type),
            source_chunk_ids=sorted(a.source_chunk_ids),
        )
        for a in merged.values()
    ]


async def _embed_entities(
    accums: list[_EntityAccum],
    *,
    agent: str,
    tenant_id: str,
    embedding_model: str,
    api_key: str | None,
    project_id: str | None = None,
) -> list[Entity]:
    full_model = qualified_model_name(embedding_model)
    # Embed "name: description" so the seed vector captures both the label
    # and its gloss; falls back to just the name when no description.
    texts = [a.name if not a.description else f"{a.name}: {a.description}" for a in accums]
    embeddings: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH_SIZE):
        batch = texts[i : i + _EMBED_BATCH_SIZE]
        embeddings.extend(await embed_texts(batch, model=embedding_model, api_key=api_key))
    return [
        Entity(
            entity_id=a.entity_id,
            tenant_id=tenant_id,
            agent=agent,
            project_id=project_id,
            name=a.name,
            type=a.type,
            description=a.description or None,
            embedding=emb,
            embedding_model=full_model,
            content_hash=_entity_hash(agent, tenant_id, a.name, a.type),
            source_chunk_ids=sorted(a.source_chunk_ids),
        )
        for a, emb in zip(accums, embeddings, strict=True)
    ]


def _clamp_weight(value: Any) -> float:
    try:
        w = float(value)
    except (TypeError, ValueError):
        return 0.5  # neutral default when the model omits / malforms weight
    return max(0.0, min(1.0, w))


def _default_complete_fn(*, model: str, api_key: str | None, timeout_s: float) -> CompleteFn:
    async def _call(prompt: str) -> str:
        import litellm  # noqa: PLC0415 — lazy, same as multi_hop / rewrite

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "num_retries": 0,
            "timeout": timeout_s,
            "max_tokens": 1500,
            "temperature": 0.0,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        resp = await litellm.acompletion(**kwargs)
        return _extract_content(resp)

    return _call


def _extract_content(resp: Any) -> str:
    """Pull text content from a LiteLLM response. Defensive — any
    structural surprise returns ``""`` (mirrors multi_hop)."""
    try:
        content = resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_extraction(content: str) -> dict[str, Any] | None:
    """Tolerant JSON parse of the extractor's output. Strips ``` fences and
    falls back to the first {...} block. Returns ``None`` on failure."""
    stripped = content.strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
        stripped = stripped.strip()
    parsed = _try_json(stripped)
    if parsed is None:
        match = _JSON_BLOCK_RE.search(stripped)
        if match:
            parsed = _try_json(match.group(0))
    if not isinstance(parsed, dict):
        return None
    return parsed


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
