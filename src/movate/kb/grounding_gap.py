"""Grounding-gap detector — "RAG-shaped agent, empty KB" (D7c / #134).

A common confusing state: an agent is **RAG-shaped** (it declares a
``kb-vector-lookup`` skill, and/or opts into ADR-023 pre-retrieval via
``retrieval.auto_into``) but its **knowledge base is EMPTY** — no chunks
were ever ingested. The agent then retrieves nothing and answers
ungrounded, with no obvious signal as to why.

This module is the small, reusable detector behind D7c's proactive offer.
It is *detection only* — the surfaces that consume it (``mdk validate``'s
warning, ``mdk dev``'s offer) own the messaging and the delegation to the
shipped ``mdk kb ingest`` path. There is no ingest logic here.

Boundaries (CLAUDE.md rule 6): the emptiness probe consumes storage through
the :class:`~movate.storage.base.StorageProvider` Protocol, never a concrete
backend. The RAG-shape check reads only the declarative
:class:`~movate.core.models.AgentSpec`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from movate.core.models import AgentSpec
    from movate.storage.base import StorageProvider

# The tenant a local (pre-deploy) KB is scoped under. Mirrors the constant
# the rest of the local KB path uses (``mdk kb ingest`` defaults, the
# ADR-023 pre-retrieval probe, the F7 acceptance harness).
LOCAL_TENANT = "local"


def is_rag_shaped(spec: AgentSpec) -> bool:
    """True when ``spec`` is configured to retrieve from a vector KB.

    Two independent markers, either of which makes an agent RAG-shaped:

    1. it declares a ``kb-vector`` skill (the ``kb-vector-lookup`` skill the
       agent invokes during a tool-use loop), or
    2. it opts into ADR-023 declarative pre-retrieval
       (``retrieval.auto_into`` is set — ``auto_retrieval_enabled``).

    Either path means the agent expects to ground its answers on ingested
    chunks; with an empty KB both silently return nothing. We deliberately
    check the spec's *declared* skill names (not the resolved skills) so the
    detector works off a plain :class:`AgentSpec` with no project on disk.
    """
    if any("kb-vector" in name.lower() for name in spec.skills):
        return True
    return spec.retrieval.auto_retrieval_enabled


async def kb_is_empty(
    storage: StorageProvider,
    *,
    agent: str,
    tenant_id: str = LOCAL_TENANT,
) -> bool:
    """True when ``agent``'s KB (for ``tenant_id``) holds zero chunks.

    Reuses the canonical chunk-list path
    (:meth:`StorageProvider.list_kb_chunks`) with ``limit=1`` — we only need
    to know whether *any* chunk exists, not enumerate them. The caller owns
    storage lifecycle (``init`` / ``close``); this function only queries.
    """
    chunks = await storage.list_kb_chunks(agent=agent, tenant_id=tenant_id, limit=1)
    return len(chunks) == 0


async def has_grounding_gap(
    spec: AgentSpec,
    storage: StorageProvider,
    *,
    tenant_id: str = LOCAL_TENANT,
) -> bool:
    """True when ``spec`` is RAG-shaped AND its KB is empty (the D7c gap).

    The single predicate the surfaces gate their offer on. Short-circuits on
    the RAG-shape check, so a non-RAG agent never touches storage — keeping
    the dominant non-RAG path free of any KB query (and any new output).
    """
    if not is_rag_shaped(spec):
        return False
    return await kb_is_empty(storage, agent=spec.name, tenant_id=tenant_id)


__all__ = ["LOCAL_TENANT", "has_grounding_gap", "is_rag_shaped", "kb_is_empty"]
