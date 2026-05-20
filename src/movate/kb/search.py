"""Search pipeline: question text → top-K retrieved chunks.

Four composable stages (each opt-in, compounding):

* **Vector-only** (default) — embed the question via the same model
  used at ingest time, return cosine-ranked chunks. Best for
  paraphrase-heavy questions where word identity doesn't match the
  KB's wording.
* **Hybrid** (``hybrid=True``) — run vector + BM25 lexical search in
  parallel, then fuse the rankings with RRF. Typically 15-25% better
  recall on real corpora, especially for queries containing rare
  terms (product names, error codes, citation IDs) that vector
  retrieval blurs out.
* **Query rewriting** (``rewrite_variants > 0``) — expand the
  original question into N paraphrases via a small LLM, run retrieval
  for each variant, dedup by chunk_id, then fuse. Catches the case
  where the user's wording doesn't match the KB's terminology even
  for the lexical path (e.g. "refunds?" → KB chunks talking about
  "return policy"). Stacks with ``hybrid=True``.
* **LLM rerank** (``rerank=True``) — fetch a wider candidate pool
  from the upstream stages (``limit * rerank_candidate_multiplier``),
  then ask a small LLM to score each candidate's relevance to the
  query. The reranker corrects "noisy top-K" — chunks with high
  cosine/BM25 scores that don't actually answer the question.
  The third stage in the standard retrieve → rerank → generate
  pipeline.

Powers ``mdk kb search`` (the CLI command) AND the
``kb-vector-lookup`` skill (invoked at agent run time).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from movate.core.models import KbChunkWithScore
from movate.kb.embed import (
    DEFAULT_EMBEDDING_MODEL,
    embed_texts,
)
from movate.kb.lexical import rrf_fuse

if TYPE_CHECKING:
    from movate.kb.trace import SearchTrace


async def search(  # noqa: PLR0912 — orchestrator naturally branches across stages
    *,
    storage: object,
    question: str,
    agent: str,
    tenant_id: str,
    limit: int = 5,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    api_key: str | None = None,
    hybrid: bool = False,
    fetch_multiplier: int = 4,
    rewrite_variants: int = 0,
    rewriter_model: str | None = None,
    rerank: bool = False,
    rerank_model: str | None = None,
    rerank_candidate_multiplier: int = 3,
    multi_hop: int = 0,
    multi_hop_model: str | None = None,
    multi_hop_max_total_chunks: int = 15,
    trace: SearchTrace | None = None,
) -> list[KbChunkWithScore]:
    """Embed ``question`` + return the top-``limit`` chunks ranked.

    Modes (composable — flags can be combined):

    * ``hybrid=False``, ``rewrite_variants=0``, ``rerank=False``
      (default): pure vector / cosine similarity via the storage
      layer.
    * ``hybrid=True``: fetch ``limit * fetch_multiplier`` candidates
      via BOTH vector and BM25 lexical paths, then fuse with
      reciprocal rank fusion (RRF) and return the top ``limit``.
      The multiplier ensures the fusion has enough candidates from
      each path to find the cross-method overlap that RRF rewards.
      Default ``4`` fetches 20 per path for a 5-result query —
      proven sweet spot.
    * ``rewrite_variants > 0``: expand the question into N paraphrases
      via a small LLM, run the configured retrieval (vector or
      hybrid) for the original AND each variant, then RRF-fuse the
      N+1 ranked lists. The rewriter never blocks retrieval — on
      any LLM failure we degrade to single-query behavior with a
      warning log. Capped at :data:`movate.kb.rewrite.MAX_VARIANTS`.
    * ``rerank=True``: fetch ``limit * rerank_candidate_multiplier``
      candidates from the upstream stages, then ask a small LLM
      to score each by relevance and return the top ``limit``.
      Catches "noisy top-K" — chunks the upstream stages rank high
      that don't actually answer the question. ~200ms latency
      + ~$0.0002/query overhead. Falls back to upstream order on
      any LLM failure.
    * ``multi_hop > 0``: iterative retrieve → reason → retrieve loop.
      Each hop runs the full pipeline (the other stages) for the
      current sub-query, then a planner LLM decides "done" or
      generates a refined sub-query. Best on questions that chain
      multiple facts ("how does X interact with Y?"). Caps at
      ``multi_hop`` hops + ``multi_hop_max_total_chunks`` aggregated
      chunks. Falls back to single-pass on planner failure.

    The ``embedding_model`` MUST match what was used at ingest time —
    different models produce incomparable vector spaces. The storage
    layer raises :class:`ValueError` on dim-mismatch, so a wrong-model
    query fails loudly rather than returning garbage. The default is
    the same default as ingest, so the common case just works.
    """
    if not question.strip():
        return []

    # Multi-hop wraps the rest of the pipeline. Each hop runs
    # rewriter + retrieval + RRF + rerank (whatever's enabled) for
    # the CURRENT sub-query, then asks the planner LLM whether to
    # continue. Composes with all the other stages — each hop gets
    # the full retrieval pipeline.
    if multi_hop > 0:
        from movate.kb.multi_hop import (  # noqa: PLC0415 — lazy import
            DEFAULT_TERMINATION_MODEL,
            multi_hop_search,
        )

        # Per-hop counter for nested trace stage names. The outer
        # multi-hop loop calls _one_hop once per sub-query; each
        # invocation's stages get a ``hop_N:`` prefix so the trace
        # reads in source order. The planner's own latency is folded
        # into the gap between hops (best we can do without
        # instrumenting the multi_hop_search internals).
        hop_idx = [0]

        async def _one_hop(sub_query: str) -> list[KbChunkWithScore]:
            # Recurse into search() but with multi_hop=0 to avoid
            # infinite recursion. Each hop gets the full retrieval
            # stack (vector / hybrid / rewriter / rerank) tuned by
            # the same flags the caller passed. When a trace is
            # active we wrap the call with a per-hop timer so the
            # operator sees per-hop cost; the nested stages inside
            # search() get an "outer" record on the trace's stages
            # list because the same trace object is reused (we
            # rename them after-the-fact for readability).
            if trace is None:
                inner_trace = None
            else:
                from movate.kb.trace import SearchTrace as _SearchTrace  # noqa: PLC0415

                inner_trace = _SearchTrace()
            result = await search(
                storage=storage,
                question=sub_query,
                agent=agent,
                tenant_id=tenant_id,
                limit=limit,
                embedding_model=embedding_model,
                api_key=api_key,
                hybrid=hybrid,
                fetch_multiplier=fetch_multiplier,
                rewrite_variants=rewrite_variants,
                rewriter_model=rewriter_model,
                rerank=rerank,
                rerank_model=rerank_model,
                rerank_candidate_multiplier=rerank_candidate_multiplier,
                multi_hop=0,  # break recursion
                trace=inner_trace,
            )
            # Fold the inner trace's stages into the outer trace
            # under a hop-prefixed name so the operator can see
            # the per-hop breakdown.
            if trace is not None and inner_trace is not None:
                this_hop = hop_idx[0]
                for stage in inner_trace.stages:
                    trace.record(
                        f"hop_{this_hop}:{stage.name}",
                        stage.duration_ms,
                        input_count=stage.input_count,
                        output_count=stage.output_count,
                        details={**stage.details, "sub_query": sub_query},
                        chunk_ids=stage.chunk_ids,
                    )
                hop_idx[0] = this_hop + 1
            return result

        return await multi_hop_search(
            question=question,
            retrieve_fn=_one_hop,
            max_hops=multi_hop,
            max_total_chunks=multi_hop_max_total_chunks,
            model=multi_hop_model or DEFAULT_TERMINATION_MODEL,
            api_key=api_key,
        )

    # Query rewriting expands ``question`` into N+1 variants. The
    # original is always the first variant, even when the rewriter
    # call fails — so this branch reduces to "[question]" in the
    # default (rewrite_variants=0) case.
    variants = [question]
    if rewrite_variants > 0:
        from movate.kb.rewrite import (  # noqa: PLC0415 — lazy import keeps litellm off the default hot path
            DEFAULT_REWRITER_MODEL,
            rewrite_query,
        )

        if trace is not None:
            with trace.time("rewrite") as rec:
                variants = await rewrite_query(
                    question,
                    n=rewrite_variants,
                    model=rewriter_model or DEFAULT_REWRITER_MODEL,
                    api_key=api_key,
                )
                rec.output_count = len(variants)
                rec.details["variants"] = list(variants)
                rec.details["requested"] = rewrite_variants
        else:
            variants = await rewrite_query(
                question,
                n=rewrite_variants,
                model=rewriter_model or DEFAULT_REWRITER_MODEL,
                api_key=api_key,
            )
        if not variants:
            variants = [question]

    # When reranking is enabled, fetch a wider candidate pool from
    # the upstream stages so the reranker has options to choose
    # from. Default 3x means a 5-result query collects 15 candidates,
    # which the LLM scores down to the final 5.
    upstream_limit = limit * rerank_candidate_multiplier if rerank else limit

    # Fan-out retrieval: run the configured pipeline once per
    # variant. For the single-variant case (default), this is
    # exactly equivalent to the previous behavior — no LLM call,
    # one retrieval pass.
    per_variant_results: list[list[KbChunkWithScore]] = []
    for i, variant in enumerate(variants):
        if trace is not None:
            with trace.time(f"retrieve[{i}]", variant=variant) as rec:
                results = await _retrieve_one(
                    storage=storage,
                    question=variant,
                    agent=agent,
                    tenant_id=tenant_id,
                    limit=upstream_limit,
                    embedding_model=embedding_model,
                    api_key=api_key,
                    hybrid=hybrid,
                    fetch_multiplier=fetch_multiplier,
                )
                rec.output_count = len(results)
                rec.details["mode"] = "hybrid" if hybrid else "vector"
                # PR-S: stamp the per-stage chunk path so operators
                # can answer "where did chunk X drop out?".
                rec.chunk_ids = [r.chunk.chunk_id for r in results]
        else:
            results = await _retrieve_one(
                storage=storage,
                question=variant,
                agent=agent,
                tenant_id=tenant_id,
                limit=upstream_limit,
                embedding_model=embedding_model,
                api_key=api_key,
                hybrid=hybrid,
                fetch_multiplier=fetch_multiplier,
            )
        per_variant_results.append(results)

    # Fuse across variants (single-variant case skips the round-trip).
    # Output of THIS stage is the candidate set for the reranker
    # (when enabled) — so we keep the wider ``upstream_limit`` here,
    # not the final ``limit``.
    if len(per_variant_results) == 1:
        upstream_results = per_variant_results[0][:upstream_limit]
    elif trace is not None:
        with trace.time("rrf_fuse") as rec:
            rec.input_count = sum(len(v) for v in per_variant_results)
            upstream_results = rrf_fuse(*per_variant_results, limit=upstream_limit)
            rec.output_count = len(upstream_results)
            rec.details["variants"] = len(per_variant_results)
            rec.chunk_ids = [r.chunk.chunk_id for r in upstream_results]
    else:
        # Multi-variant case: RRF-fuse across all variant result
        # lists. Chunks that match multiple variants accumulate
        # score, so the "agrees across paraphrases" signal floats
        # to the top.
        upstream_results = rrf_fuse(*per_variant_results, limit=upstream_limit)

    # Final stage: LLM rerank. The reranker scores each candidate's
    # relevance to the ORIGINAL question (not rewritten variants —
    # the user's original phrasing is the source of truth for what
    # they want answered).
    if rerank and upstream_results:
        from movate.kb.rerank import (  # noqa: PLC0415 — lazy import keeps litellm off the default hot path
            DEFAULT_RERANKER_MODEL,
            llm_rerank,
        )

        if trace is not None:
            # Capture the pre-rerank top-K so the trace can show
            # how much the rerank shuffled things — the operator's
            # eye-test is "did the rerank actually help?"
            pre_rerank_top_ids = [r.chunk.chunk_id for r in upstream_results[:limit]]
            with trace.time("rerank") as rec:
                rec.input_count = len(upstream_results)
                reranked = await llm_rerank(
                    question=question,
                    candidates=upstream_results,
                    limit=limit,
                    model=rerank_model or DEFAULT_RERANKER_MODEL,
                    api_key=api_key,
                )
                rec.output_count = len(reranked)
                post_rerank_top_ids = [r.chunk.chunk_id for r in reranked]
                rec.chunk_ids = post_rerank_top_ids
                # Overlap between pre-rerank and post-rerank top-K.
                # 100% = rerank changed nothing; 0% = rerank totally
                # replaced the top-K.
                pre_set = set(pre_rerank_top_ids)
                post_set = set(post_rerank_top_ids)
                if pre_set:
                    overlap = len(pre_set & post_set) / len(pre_set)
                    rec.details["top_k_overlap"] = round(overlap, 2)
            return reranked

        return await llm_rerank(
            question=question,
            candidates=upstream_results,
            limit=limit,
            model=rerank_model or DEFAULT_RERANKER_MODEL,
            api_key=api_key,
        )

    # No rerank: clamp the upstream results to the final limit
    # and return.
    return upstream_results[:limit]


async def _retrieve_one(
    *,
    storage: object,
    question: str,
    agent: str,
    tenant_id: str,
    limit: int,
    embedding_model: str,
    api_key: str | None,
    hybrid: bool,
    fetch_multiplier: int,
) -> list[KbChunkWithScore]:
    """Run one retrieval pass — either vector-only or hybrid.

    Factored out of :func:`search` so the fan-out path can call it
    once per query variant without duplicating the vector + BM25 +
    RRF wiring.
    """
    [query_embedding] = await embed_texts(
        [question],
        model=embedding_model,
        api_key=api_key,
    )

    if not hybrid:
        result: list[KbChunkWithScore] = await storage.search_kb_chunks(  # type: ignore[attr-defined]
            agent=agent,
            tenant_id=tenant_id,
            query_embedding=query_embedding,
            limit=limit,
        )
        return result

    # Hybrid path: fetch a wider candidate set from BOTH the vector
    # path AND the lexical (BM25) path, then fuse with RRF. The
    # multiplier widens each path's individual top-K so the fusion
    # has enough overlap to be useful — a 5-result query fetches
    # 20 from each path by default.
    candidate_limit = max(limit, int(limit * fetch_multiplier))

    # Vector path: same as the default branch above, just with a
    # wider limit.
    vector_results: list[KbChunkWithScore] = await storage.search_kb_chunks(  # type: ignore[attr-defined]
        agent=agent,
        tenant_id=tenant_id,
        query_embedding=query_embedding,
        limit=candidate_limit,
    )

    # Lexical path: delegate to the storage-native FTS index (FTS5
    # for SQLite, tsvector+GIN for Postgres, Python BM25 fallback
    # for InMemory). The storage layer handles corpus-wide scoring
    # and returns pre-ranked results, so we no longer need to load
    # all chunks into Python memory.
    lexical_results: list[KbChunkWithScore] = await storage.search_kb_chunks_lexical(  # type: ignore[attr-defined]
        agent=agent,
        tenant_id=tenant_id,
        query=question,
        limit=candidate_limit,
    )

    # Fuse + clamp to final limit. RRF ignores score scale + only
    # looks at rank, so it doesn't matter that vector scores are 0-1
    # while BM25 scores can exceed 1.
    return rrf_fuse(vector_results, lexical_results, limit=limit)
