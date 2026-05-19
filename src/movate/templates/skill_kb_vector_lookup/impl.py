"""Implementation for the __SKILL_NAME__ skill — semantic KB lookup.

Wraps :func:`movate.kb.search.search` so the agent can retrieve its
own context at run time. The chunks were ingested earlier via
``mdk kb ingest <agent> <path>`` and live in the agent's
``kb_chunks`` storage rows; this skill embeds the query, ranks, and
returns the top-K.

The chunks come back with their source path + similarity score —
the agent's prompt can render citations as ``[1]`` / ``[2]`` based
on chunk position in the returned list, with the source path
available for the operator to look up the underlying document.

Operator note: the embedding model used at query time MUST match
what was used at ingest time (different models produce incomparable
vector spaces). Storage layer rejects cross-model queries with a
clear error rather than silently degrading. Default for both ingest
+ query is ``openai/text-embedding-3-small``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.core.skill_backend import SkillExecutionContext


_DEFAULT_K = 5


async def run(
    inputs: dict[str, Any], ctx: SkillExecutionContext | None = None
) -> dict[str, Any]:
    """Skill entry point. Returns ``{chunks: [...], chunks_found: N}``.

    The ``inputs`` dict comes from the LLM's tool-use call; ``ctx``
    is the runtime's skill-execution context (carries agent name,
    tenant id, the storage handle).
    """
    question = (inputs.get("question") or "").strip()
    if not question:
        return {
            "chunks": [],
            "chunks_found": 0,
            "warning": "empty question — pass a non-empty 'question' to retrieve.",
        }

    k = int(inputs.get("k") or _DEFAULT_K)

    # The skill needs the runtime's storage handle. We get it from
    # the SkillExecutionContext; if ctx isn't supplied (CLI testing
    # path) we build a fresh storage from env.
    storage = None
    agent_name = ""
    tenant_id = "local"
    if ctx is not None:
        storage = getattr(ctx, "storage", None)
        agent_name = getattr(ctx, "agent_name", "")
        tenant_id = getattr(ctx, "tenant_id", "local")
    if storage is None:
        from movate.storage import build_storage  # noqa: PLC0415

        storage = build_storage()
        await storage.init()

    # Embed query + search. ``movate.kb.search.search`` handles the
    # embedding call + storage delegation; OpenAI key is read from
    # OPENAI_API_KEY at call time.
    from movate.kb.search import search as kb_search  # noqa: PLC0415

    api_key = os.environ.get("OPENAI_API_KEY", "").strip() or None
    results = await kb_search(
        storage=storage,
        question=question,
        agent=agent_name,
        tenant_id=tenant_id,
        limit=k,
        api_key=api_key,
    )

    return {
        "chunks": [
            {
                "text": r.chunk.text,
                "source": r.chunk.source,
                "score": round(r.score, 4),
            }
            for r in results
        ],
        "chunks_found": len(results),
    }
