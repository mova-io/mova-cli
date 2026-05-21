"""``mdk kb`` — ingest + search the agent's knowledge base.

Two subcommands ship in the v0.9 RAG MVP:

* ``mdk kb ingest <agent> <path>`` — read files under ``<path>``,
  chunk them, embed via OpenAI ``text-embedding-3-small``, persist
  to the agent's ``kb_chunks`` rows. Idempotent.
* ``mdk kb search <agent> <question>`` — semantic search over the
  agent's KB. Prints the top-K chunks with similarity scores. Useful
  for tuning retrieval (chunk size, dedup behavior) without running
  the agent end-to-end.

Both commands use the local sqlite DB (``~/.movate/local.db``) by
default — same storage path the runtime uses. For Postgres-backed
deployments, set ``MOVATE_DB_URL`` and the commands transparently
route there.

The third leg — the ``kb-vector-lookup`` skill that lets the agent
retrieve at run time — lives in ``src/movate/templates/skill_kb_vector_lookup/``;
it imports from ``movate.kb.search`` under the hood.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from movate.kb.embed import DEFAULT_EMBEDDING_MODEL

console = Console()
err_console = Console(stderr=True)


kb_app = typer.Typer(
    name="kb",
    help=(
        "Knowledge-base ingest + search for the v0.9 RAG MVP. "
        "Stores chunks in the local sqlite DB (or Postgres if "
        "MOVATE_DB_URL is set); embeddings via OpenAI."
    ),
    no_args_is_help=True,
)


# Default tenant for local CLI use. Matches the convention in
# movate.core.replay + run_replay so KB ingest by `mdk kb ingest`
# is reachable by `mdk run` later without per-call tenant juggling.
_DEFAULT_TENANT = "local"

# Chunk-text truncation in the search table — keeps the table
# readable without losing too much context. ``--full`` overrides.
_CHUNK_PREVIEW_CHARS = 200

# Score-color thresholds for the search-result table. >=0.7 is a
# strong match by cosine convention; >=0.5 is plausible; below that
# is likely noise. Same buckets the LLM-judge gate uses.
_SCORE_GREEN_THRESHOLD = 0.7
_SCORE_YELLOW_THRESHOLD = 0.5


async def _build_storage() -> object:
    """Build the same storage provider the runtime + CLI use.

    Honors ``MOVATE_DB_URL`` for Postgres; falls back to sqlite at
    the default path. Calling ``init()`` is idempotent — runs
    schema migrations on every invocation.
    """
    from movate.storage import build_storage  # noqa: PLC0415

    s = build_storage()
    await s.init()
    return s


@kb_app.command("ingest")
def ingest(
    agent: str = typer.Argument(
        ...,
        help=(
            "Agent name (must match a directory under ./agents/ — "
            "we don't enforce this at the storage layer, but the "
            "skill-side lookup at run time scopes by agent so a "
            "mismatch returns no results)."
        ),
    ),
    path: Path = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help=(
            "File or directory to ingest. Directories are walked "
            "recursively; .md, .markdown, .txt files are picked up. "
            "Hidden dirs (.git, .venv) skipped."
        ),
    ),
    tenant_id: str = typer.Option(
        _DEFAULT_TENANT,
        "--tenant-id",
        help=(
            "Tenant scope. Defaults to 'local' for CLI use. Override "
            "in production where the tenant comes from the auth context."
        ),
    ),
    model: str = typer.Option(
        DEFAULT_EMBEDDING_MODEL,
        "--model",
        help=(
            "Embedding model. Bare names (``text-embedding-3-small``) and "
            "``openai/`` prefixed strings go directly to OpenAI. Any other "
            "``provider/model`` string (``cohere/embed-english-v3.0``, "
            "``voyage/voyage-3``, etc.) is routed through LiteLLM — set "
            "the matching provider env var (COHERE_API_KEY, VOYAGE_API_KEY, …)."
        ),
    ),
    api_key_env: str = typer.Option(
        "OPENAI_API_KEY",
        "--api-key-env",
        help=(
            "Env var holding the API key for embedding calls. "
            "Defaults to ``OPENAI_API_KEY``. Override when using a "
            "non-OpenAI provider (e.g. ``COHERE_API_KEY``)."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Chunk + report what WOULD be ingested without calling "
            "the embedding API or writing to storage. Use to validate chunk "
            "sizes + counts before paying for embeddings."
        ),
    ),
    clean_source: bool = typer.Option(
        False,
        "--clean-source",
        help=(
            "Delete all existing chunks for each source file before re-ingesting. "
            "Use when updating a document — ensures stale paragraphs don't persist "
            "alongside the new content. Without this flag, dedup on content_hash "
            "means deleted paragraphs remain in the KB."
        ),
    ),
    ocr_lang: str = typer.Option(
        "",
        "--ocr-lang",
        help=(
            "Tesseract language code(s) for scanned PDFs / images. "
            "Accepts Tesseract 3-letter codes; use '+' for multi-language "
            "(e.g. 'eng+fra'). Defaults to 'eng'. Sets MOVATE_OCR_LANG for "
            "this invocation only."
        ),
    ),
    ocr_backend: str = typer.Option(
        "",
        "--ocr-backend",
        help=(
            "OCR engine: 'tesseract' (default, needs pytesseract + Tesseract binary) "
            "or 'easyocr' (pure-Python, better on noisy scans, larger install). "
            "Sets MOVATE_OCR_BACKEND for this invocation only."
        ),
    ),
) -> None:
    """Ingest a knowledge-base file or directory into ``agent``'s KB.

    Use ``--dry-run`` to preview chunk count + size distribution
    without consuming any embedding budget. Useful when tuning a
    new corpus before committing to the real ingest.

    Use ``--model`` to select a non-default embedding provider, e.g.
    ``--model cohere/embed-english-v3.0``. The model used at ingest
    MUST match the model used at search time.

    Use ``--clean-source`` when updating an existing document to remove
    stale chunks before writing new ones.

    Use ``--ocr-lang`` / ``--ocr-backend`` for non-English or noisy scans.
    """
    import os  # noqa: PLC0415

    # --ocr-lang / --ocr-backend set env vars for this process only so
    # the parsers module picks them up without changing its signature.
    if ocr_lang:
        os.environ["MOVATE_OCR_LANG"] = ocr_lang
    if ocr_backend:
        os.environ["MOVATE_OCR_BACKEND"] = ocr_backend

    # --dry-run skips the API key check (no embedding calls).
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key and not dry_run:
        err_console.print(
            f"[red]✗[/red] no API key found in [bold]${api_key_env}[/bold]. "
            "Set the env var or pass [bold]--api-key-env[/bold] to point at "
            "the correct env var for your embedding provider. Pass "
            "[bold]--dry-run[/bold] to preview without embedding."
        )
        raise typer.Exit(code=2)

    if dry_run:
        # No storage writes, no embedding calls — just walk + chunk.
        # Renders the same table shape so the operator can compare
        # what they're about to pay for against the existing chunks.
        _run_dry(path=path, agent=agent)
        return

    async def _run() -> None:
        from movate.kb.ingest import ingest_path  # noqa: PLC0415

        storage = await _build_storage()
        try:
            console.print(f"[bold cyan]Ingesting[/bold cyan] {path} -> agent [bold]{agent}[/bold]…")
            if clean_source:
                console.print(
                    "[dim]--clean-source: deleting existing chunks before re-ingest[/dim]"
                )
            summaries = await ingest_path(
                storage=storage,
                path=path,
                agent=agent,
                tenant_id=tenant_id,
                embedding_model=model,
                api_key=api_key,
                clean_source=clean_source,
            )
        finally:
            await storage.close()  # type: ignore[attr-defined]

        if not summaries:
            console.print(
                "[yellow]⚠[/yellow] no ingestible files found (looked for .md / .markdown / .txt)."
            )
            return

        # Render a summary table — one row per source.
        show_removed = clean_source and any(s.chunks_removed > 0 for s in summaries)
        table = Table(title=f"[bold]Ingest summary[/bold] — agent [bold]{agent}[/bold]")
        table.add_column("source", overflow="fold")
        if show_removed:
            table.add_column("removed", justify="right")
        table.add_column("chunks", justify="right")
        table.add_column("embedding model")
        for s in summaries:
            row = [s.source]
            if show_removed:
                row.append(str(s.chunks_removed))
            row.extend([str(s.chunks_saved), s.embedding_model])
            table.add_row(*row)
        console.print(table)
        total = sum(s.chunks_saved for s in summaries)
        console.print(f"[green]✓[/green] {total} chunks saved across {len(summaries)} file(s).")
        console.print(f'[dim]Try it: [bold]mdk kb search {agent} "your question here"[/bold][/dim]')

    asyncio.run(_run())


def _print_trace_table(trace: object) -> None:
    """Render a :class:`SearchTrace` as a Rich table.

    Three columns: stage name (left-aligned), duration in ms
    (right-aligned), and a free-form details column (truncated to
    keep narrow terminals readable). A footer row shows the total.

    Designed to print BEFORE the results table so the operator
    can read top-to-bottom: timing context, then the chunks. Both
    tables print to stdout; ``--trace`` is opt-in so default output
    stays clean for piping / scripting.
    """
    # Imported locally so callers that never set ``--trace`` don't
    # pay the import cost — Rich tables aren't free at import time.
    from rich.console import Console  # noqa: PLC0415

    stdout = Console()
    table = Table(
        title="[bold]Search trace[/bold]",
        show_lines=False,
        title_justify="left",
    )
    table.add_column("stage", style="cyan", no_wrap=True)
    table.add_column("latency", justify="right", style="bold")
    table.add_column("in → out", justify="right", style="dim")
    table.add_column("top chunks", overflow="fold", max_width=40)
    table.add_column("details", overflow="fold")

    stages = getattr(trace, "stages", []) or []
    for stage in stages:
        # Show "→ N" when input_count is 0 — it's the stage's first
        # source of candidates, not a transformation of an upstream
        # count.
        if stage.input_count:
            io = f"{stage.input_count} → {stage.output_count}"
        else:
            io = f"→ {stage.output_count}"
        # Per-chunk path (PR-S). Operators can read down the column
        # to see which chunks survived each stage; "where did chunk X
        # drop out?" is now answerable by inspection.
        chunk_path = _format_chunk_path(stage.chunk_ids)
        # Compact the details dict into one line. Skip noisy
        # internals (full variant lists, full sub-query strings).
        details_str = _format_stage_details(stage.details)
        table.add_row(
            stage.name,
            f"{stage.duration_ms:.1f}ms",
            io,
            chunk_path,
            details_str,
        )

    total_ms = getattr(trace, "total_ms", lambda: 0.0)()
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{total_ms:.1f}ms[/bold]",
        "",
        "",
        "",
    )
    stdout.print(table)


_MAX_DETAILS_LEN = 80
# Per-stage chunk-path summary cap. Showing more than the top N
# chunk-id prefixes per stage spams the table; the full list is
# still on the trace object for programmatic readers.
_CHUNK_PATH_TOP_N = 3
_CHUNK_ID_PREFIX = 8


def _format_chunk_path(chunk_ids: list[str] | None) -> str:
    """Render a stage's chunk-id list compactly for the trace table.

    Shows the first ``_CHUNK_PATH_TOP_N`` chunk ids (truncated to
    ``_CHUNK_ID_PREFIX`` chars each) with a "+N more" tail when
    the stage produced more. ``None`` (the rewriter and other
    non-chunk stages) renders as a placeholder.
    """
    if chunk_ids is None:
        return "[dim]—[/dim]"
    if not chunk_ids:
        return "[dim](empty)[/dim]"
    head = [c[:_CHUNK_ID_PREFIX] for c in chunk_ids[:_CHUNK_PATH_TOP_N]]
    suffix = f" +{len(chunk_ids) - _CHUNK_PATH_TOP_N}" if len(chunk_ids) > _CHUNK_PATH_TOP_N else ""
    return ", ".join(head) + suffix


def _format_stage_details(details: dict[str, object]) -> str:
    """Compact a stage's details dict for table display.

    Drops keys whose value is too long for a one-liner; keeps
    scalars + small collections. Best-effort — the full details
    are still on the trace object for programmatic inspection.
    """
    if not details:
        return ""
    parts: list[str] = []
    for k, v in details.items():
        # Skip long variant / sub-query lists; the operator gets
        # the count from input_count/output_count, the actual
        # strings are noise here.
        if isinstance(v, list) and len(v) > 0:
            parts.append(f"{k}={len(v)}")
            continue
        s = str(v)
        if len(s) > _MAX_DETAILS_LEN:
            s = s[:_MAX_DETAILS_LEN] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _run_dry(*, path: Path, agent: str) -> None:
    """Dry-run path for ``mdk kb ingest --dry-run``.

    Walks the same files the real path would, chunks them with the
    same splitter, but writes nothing + calls no APIs. Renders a
    table showing per-file chunk counts + a rough embedding-cost
    estimate so the operator can decide whether to commit.
    """
    from movate.kb.chunk import split_paragraphs  # noqa: PLC0415
    from movate.kb.ingest import find_files  # noqa: PLC0415

    files = find_files(path)
    if not files:
        err_console.print(
            "[yellow]⚠[/yellow] no ingestible files found (looked for .md / .markdown / .txt)."
        )
        return

    table = Table(
        title=(
            f"[bold]Dry-run[/bold] — would ingest into agent [bold]{agent}[/bold] "
            "[dim](no API calls, no storage writes)[/dim]"
        )
    )
    table.add_column("source", overflow="fold")
    table.add_column("chunks", justify="right")
    table.add_column("chars", justify="right")
    table.add_column("avg chunk len", justify="right", style="dim")

    total_chunks = 0
    total_chars = 0
    for file_path in files:
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            err_console.print(f"[red]✗[/red] could not read {file_path}: {exc}")
            continue
        chunks = split_paragraphs(text, source=str(file_path))
        chars = sum(len(c.text) for c in chunks)
        avg = (chars / len(chunks)) if chunks else 0
        table.add_row(
            str(file_path),
            str(len(chunks)),
            f"{chars:,}",
            f"{avg:.0f}",
        )
        total_chunks += len(chunks)
        total_chars += chars

    console.print(table)

    # Rough embedding-cost estimate (text-embedding-3-small pricing:
    # $0.02 per 1M input tokens; ~4 chars per token English average).
    # The actual cost depends on the exact text the model tokenizes;
    # this is a within-10% ballpark for typical markdown.
    est_tokens = total_chars / 4
    est_cost_usd = est_tokens * 0.02 / 1_000_000
    console.print(
        f"\n[bold]Estimated[/bold]: {total_chunks} chunks, "
        f"{total_chars:,} chars (~{est_tokens:,.0f} tokens), "
        f"~[bold]${est_cost_usd:.5f}[/bold] in embeddings cost."
    )
    console.print(
        "[dim]To commit: rerun without [bold]--dry-run[/bold] "
        "(writes to storage + embeds via OpenAI).[/dim]"
    )


@kb_app.command("search")
def search(
    agent: str = typer.Argument(
        ...,
        help="Agent whose KB to search.",
    ),
    question: str = typer.Argument(
        ...,
        help="Free-text question to retrieve against.",
    ),
    k: int = typer.Option(
        5,
        "--k",
        "-k",
        min=1,
        max=50,
        help="Number of top results to return.",
    ),
    tenant_id: str = typer.Option(
        _DEFAULT_TENANT,
        "--tenant-id",
        help="Tenant scope (matches the value used at ingest).",
    ),
    model: str = typer.Option(
        DEFAULT_EMBEDDING_MODEL,
        "--model",
        help=(
            "Embedding model used to embed the query. MUST match the model "
            "used at ingest time — different models produce incomparable "
            "vector spaces. Bare names (``text-embedding-3-small``) and "
            "``openai/`` prefixed strings go directly to OpenAI; any other "
            "``provider/model`` string is routed through LiteLLM."
        ),
    ),
    api_key_env: str = typer.Option(
        "OPENAI_API_KEY",
        "--api-key-env",
        help=(
            "Env var holding the API key for query embedding. "
            "Defaults to ``OPENAI_API_KEY``; override for non-OpenAI providers."
        ),
    ),
    show_full: bool = typer.Option(
        False,
        "--full",
        help="Print full chunk text (default truncates to 200 chars).",
    ),
    hybrid: bool = typer.Option(
        False,
        "--hybrid",
        help=(
            "Combine vector + BM25 lexical search via reciprocal rank "
            "fusion. Typically 15-25% better recall on real corpora — "
            "vector catches paraphrase, BM25 catches rare-term hits. "
            "No extra API cost (BM25 runs locally)."
        ),
    ),
    rewrite: int = typer.Option(
        0,
        "--rewrite",
        min=0,
        max=8,
        help=(
            "Expand the query into N alternative paraphrases via a "
            "small LLM, run retrieval for each, fuse the rankings "
            "with RRF. Catches vague queries that miss specific KB "
            "terminology (e.g. 'refunds?' → KB chunks talking about "
            "'return policy'). Adds ~200ms latency + ~$0.0001/query. "
            "Stacks with --hybrid. 0 = disabled (default)."
        ),
    ),
    rerank: bool = typer.Option(
        False,
        "--rerank",
        help=(
            "Add a rerank stage that re-scores upstream candidates "
            "by relevance to the question, correcting 'noisy top-K' "
            "where vector/BM25 scores rank irrelevant chunks high. "
            "Fetches 3x candidates upstream then trims to top-K. "
            "Use --rerank-mode to choose between LLM (default) and "
            "local cross-encoder backends. Stacks with --hybrid and --rewrite."
        ),
    ),
    rerank_mode: str = typer.Option(
        "llm",
        "--rerank-mode",
        help=(
            "Which rerank backend to use when --rerank is set. "
            "'llm' (default) — one batched LLM call via LiteLLM "
            "(~200ms, ~$0.0002/query, zero extra deps). "
            "'cross_encoder' — local sentence-transformers cross-encoder "
            "(~50ms CPU, zero API cost, requires "
            "'pip install movate-cli[cross-encoder]' ~300MB)."
        ),
    ),
    multi_hop: int = typer.Option(
        0,
        "--multi-hop",
        min=0,
        max=5,
        help=(
            "Iterative retrieve → reason → retrieve loop. Each hop "
            "runs the full retrieval pipeline (--hybrid / --rewrite / "
            "--rerank apply per-hop), then a planner LLM decides "
            "'done' or generates a refined sub-query. Best on multi-fact "
            "questions ('how does X interact with Y?'). Adds N "
            "planner calls + N retrieval passes. 0 = disabled (default)."
        ),
    ),
    show_trace: bool = typer.Option(
        False,
        "--trace",
        help=(
            "Render a per-stage trace table after the results: which "
            "stages fired, how long each took, candidate counts in/out. "
            "Useful for debugging 'why didn't this chunk surface?' or "
            "'where's my latency going?'. Adds ~0.1ms of overhead per "
            "stage (negligible)."
        ),
    ),
) -> None:
    """Semantic search over ``agent``'s KB. Prints top-K with scores.

    Use this to validate that retrieval is finding the right chunks
    BEFORE running the agent end-to-end — saves the cost of agent
    iterations on a bad KB.

    Default mode is vector-only (cosine similarity over OpenAI
    embeddings). ``--hybrid`` adds a parallel BM25 lexical search
    + reciprocal rank fusion; recommended for queries containing
    product names, error codes, or other rare terms. ``--rewrite N``
    fans out across N+1 LLM-generated paraphrases — best on vague
    or under-specified questions.
    """
    import os  # noqa: PLC0415

    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        err_console.print(
            f"[red]✗[/red] no API key found in [bold]${api_key_env}[/bold]. "
            "Set the env var or pass [bold]--api-key-env[/bold] to point at "
            "the correct env var for your embedding provider."
        )
        raise typer.Exit(code=2)

    async def _run() -> None:
        from movate.kb.search import search as kb_search  # noqa: PLC0415
        from movate.kb.trace import SearchTrace  # noqa: PLC0415

        trace = SearchTrace() if show_trace else None

        storage = await _build_storage()
        try:
            results = await kb_search(
                storage=storage,
                question=question,
                agent=agent,
                tenant_id=tenant_id,
                limit=k,
                api_key=api_key,
                embedding_model=model,
                hybrid=hybrid,
                rewrite_variants=rewrite,
                rerank=rerank,
                rerank_mode=rerank_mode,
                multi_hop=multi_hop,
                trace=trace,
            )
        finally:
            await storage.close()  # type: ignore[attr-defined]

        # Render the trace before the results table so the operator
        # sees timing context above the chunks (which can be long).
        # Hide noisy details from the table; full payload is on the
        # ``trace`` object for programmatic callers.
        if trace is not None and trace.stages:
            _print_trace_table(trace)

        if not results:
            err_console.print(
                f"[yellow]⚠[/yellow] no chunks in [bold]{agent}[/bold]'s KB "
                f"(tenant=[bold]{tenant_id}[/bold]). "
                "Did you run [bold]mdk kb ingest[/bold] first?"
            )
            return

        mode_parts = ["hybrid" if hybrid else "vector"]
        if rewrite > 0:
            mode_parts.append(f"rewrite={rewrite}")
        if rerank:
            mode_parts.append("rerank")
        if multi_hop > 0:
            mode_parts.append(f"multi-hop={multi_hop}")
        mode_label = f"[bold magenta]{' + '.join(mode_parts)}[/bold magenta]"
        table = Table(
            title=(
                f'[bold]Top {len(results)} chunks[/bold] for "[italic]{question}[/italic]"'
                f" — agent [bold]{agent}[/bold] ({mode_label})"
            ),
            show_lines=True,
        )
        table.add_column("rank", justify="right", style="dim", no_wrap=True)
        table.add_column("score", justify="right", style="bold")
        table.add_column("source", overflow="fold", max_width=40)
        table.add_column("text", overflow="fold")
        for i, r in enumerate(results, start=1):
            text_preview = (
                r.chunk.text
                if show_full or len(r.chunk.text) <= _CHUNK_PREVIEW_CHARS
                else r.chunk.text[:_CHUNK_PREVIEW_CHARS].rstrip() + "…"
            )
            # Short source name (last path segment) — full path is in
            # the table title's tooltip if Rich's terminal supports it.
            short_source = Path(r.chunk.source).name if r.chunk.source else "?"
            score_color = (
                "green"
                if r.score >= _SCORE_GREEN_THRESHOLD
                else "yellow"
                if r.score >= _SCORE_YELLOW_THRESHOLD
                else "red"
            )
            table.add_row(
                str(i),
                f"[{score_color}]{r.score:.3f}[/{score_color}]",
                short_source,
                text_preview,
            )
        console.print(table)

    asyncio.run(_run())


@kb_app.command("list")
def list_chunks(
    agent: str = typer.Argument(..., help="Agent whose KB to inspect."),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Filter to chunks from a specific source path (file URI).",
    ),
    limit: int = typer.Option(
        50,
        "--limit",
        min=1,
        max=1000,
        help="Max rows to render. Defaults to 50; bump for full dumps.",
    ),
    tenant_id: str = typer.Option(
        _DEFAULT_TENANT,
        "--tenant-id",
        help="Tenant scope (matches the value used at ingest).",
    ),
) -> None:
    """List chunks in ``agent``'s KB. Useful for debugging
    "is my content actually in there?" without dropping into SQL.
    """

    async def _run() -> None:
        storage = await _build_storage()
        try:
            chunks = await storage.list_kb_chunks(  # type: ignore[attr-defined]
                agent=agent,
                tenant_id=tenant_id,
                source=source,
                limit=limit,
            )
        finally:
            await storage.close()  # type: ignore[attr-defined]

        if not chunks:
            err_console.print(
                f"[yellow]⚠[/yellow] no chunks for agent [bold]{agent}[/bold] "
                f"(tenant=[bold]{tenant_id}[/bold]). "
                "Run [bold]mdk kb ingest[/bold] first."
            )
            return

        table = Table(
            title=(
                f"[bold]KB chunks[/bold] — agent [bold]{agent}[/bold] "
                f"[dim]({len(chunks)} shown)[/dim]"
            ),
            show_lines=True,
        )
        table.add_column("#", justify="right", style="dim", no_wrap=True)
        table.add_column("source", overflow="fold", max_width=40)
        table.add_column("len", justify="right", style="dim", no_wrap=True)
        table.add_column("ocr", justify="center", style="dim", no_wrap=True)
        table.add_column("preview", overflow="fold")
        for i, c in enumerate(chunks, start=1):
            short = Path(c.source).name if c.source else "?"
            preview = (
                c.text
                if len(c.text) <= _CHUNK_PREVIEW_CHARS
                else c.text[:_CHUNK_PREVIEW_CHARS].rstrip() + "…"
            )
            ocr_flag = "✓" if c.ocr else ""
            table.add_row(str(i), short, str(len(c.text)), ocr_flag, preview)
        console.print(table)

    asyncio.run(_run())


@kb_app.command("stats")
def stats(
    agent: str = typer.Argument(..., help="Agent whose KB to summarize."),
    tenant_id: str = typer.Option(
        _DEFAULT_TENANT,
        "--tenant-id",
        help="Tenant scope (matches ingest value).",
    ),
    by_source: bool = typer.Option(
        False,
        "--by-source",
        help=(
            "Distribution-view: sort per-source rows by chunk count "
            "DESCENDING + add a percentage column. Useful for triaging "
            "KB quality — 'which doc dominates retrieval?' and 'is any "
            "one source contributing most of the chunks?'. Default "
            "sort (without this flag) is alphabetical by source path."
        ),
    ),
    top: int = typer.Option(
        0,
        "--top",
        min=0,
        help=(
            "Cap the per-source table at the top N rows (most chunks "
            "first when combined with --by-source). 0 = show all. "
            "Useful when an agent's KB has hundreds of source files."
        ),
    ),
) -> None:
    """Summary stats for ``agent``'s KB: chunk count, source
    breakdown, embedding model(s) in use, total + per-source character
    counts. Useful for sanity-checking after a big ingest.

    Use ``--by-source`` to flip the per-source table into a
    distribution view (sorted by chunk count DESC with a %-of-total
    column) — quick triage for 'is one document dominating?'.
    """

    async def _run() -> None:
        storage = await _build_storage()
        try:
            # Pull ALL chunks (limit 100k) for accurate aggregation.
            chunks = await storage.list_kb_chunks(  # type: ignore[attr-defined]
                agent=agent,
                tenant_id=tenant_id,
                limit=100_000,
            )
        finally:
            await storage.close()  # type: ignore[attr-defined]

        if not chunks:
            err_console.print(f"[yellow]⚠[/yellow] no chunks for agent [bold]{agent}[/bold].")
            return

        # Aggregate by source.
        per_source: dict[str, list[int]] = {}
        models: set[str] = set()
        total_chars = 0
        ocr_count = 0
        for c in chunks:
            per_source.setdefault(c.source, []).append(len(c.text))
            models.add(c.embedding_model)
            total_chars += len(c.text)
            if c.ocr:
                ocr_count += 1

        # Top-level summary.
        console.print(
            f"\n[bold]KB summary[/bold] — agent [bold]{agent}[/bold] "
            f"(tenant [dim]{tenant_id}[/dim])"
        )
        console.print(f"  total chunks: [bold]{len(chunks)}[/bold]")
        console.print(f"  total chars:  [bold]{total_chars:,}[/bold]")
        console.print(f"  sources:      [bold]{len(per_source)}[/bold]")
        console.print(f"  models:       [bold]{', '.join(sorted(models))}[/bold]")
        ocr_pct = f"{ocr_count / len(chunks) * 100:.0f}%" if ocr_count else "0%"
        console.print(
            f"  ocr chunks:   [bold]{ocr_count}[/bold] [dim]({ocr_pct} — "
            "Tesseract-extracted from scanned-image PDFs)[/dim]"
        )

        # Per-source table — sort + columns vary by --by-source.
        title = (
            "[bold]Per-source distribution[/bold] (top sources first)"
            if by_source
            else "[bold]Per-source breakdown[/bold]"
        )
        table = Table(title=title, show_lines=False)
        table.add_column("source", overflow="fold")
        table.add_column("chunks", justify="right", no_wrap=True)
        if by_source:
            table.add_column("% of total", justify="right", no_wrap=True)
        table.add_column("chars", justify="right", no_wrap=True)
        table.add_column("avg chunk len", justify="right", no_wrap=True)

        # Sort key: chunk count DESC for distribution view, alphabetical
        # for the default breakdown.
        if by_source:
            rows_iter = sorted(
                per_source.items(),
                key=lambda kv: (-len(kv[1]), kv[0]),
            )
        else:
            rows_iter = sorted(per_source.items())

        # Optional top-N cap. Applies regardless of sort mode.
        rows = list(rows_iter)
        if top > 0:
            rows = rows[:top]

        total_chunks = len(chunks)
        for source, sizes in rows:
            short = Path(source).name if source else "?"
            count = len(sizes)
            avg = sum(sizes) / count if sizes else 0
            row = [
                short,
                str(count),
            ]
            if by_source:
                pct = (count / total_chunks * 100.0) if total_chunks else 0.0
                row.append(f"{pct:.1f}%")
            row.extend([f"{sum(sizes):,}", f"{avg:.0f}"])
            table.add_row(*row)

        if top > 0 and len(per_source) > top:
            # Tail-row summary so the operator knows about the cap.
            remainder = len(per_source) - top
            row = [f"[dim]…and {remainder} more sources[/dim]", ""]
            if by_source:
                row.append("")
            row.extend(["", ""])
            table.add_row(*row)

        console.print(table)

    asyncio.run(_run())


@kb_app.command("clear")
def clear(
    agent: str = typer.Argument(..., help="Agent whose KB to clear."),
    source: str | None = typer.Option(
        None,
        "--source",
        help=("Only delete chunks from this source path. Omit to wipe the agent's entire KB."),
    ),
    tenant_id: str = typer.Option(
        _DEFAULT_TENANT,
        "--tenant-id",
        help="Tenant scope.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt (CI / scripting).",
    ),
) -> None:
    """Delete chunks from ``agent``'s KB. Use ``--source`` to remove
    just one document; omit for a full wipe. Confirmation required
    unless ``--yes`` is set."""
    target = (
        f"all chunks for agent [bold]{agent}[/bold]"
        if source is None
        else f"chunks from [bold]{source}[/bold] (agent [bold]{agent}[/bold])"
    )
    if not yes:
        from rich.prompt import Confirm  # noqa: PLC0415

        if not Confirm.ask(f"Delete {target}?", default=False):
            err_console.print("[dim]→ aborted.[/dim]")
            raise typer.Exit(code=0)

    async def _run() -> None:
        storage = await _build_storage()
        try:
            n = await storage.delete_kb_chunks(  # type: ignore[attr-defined]
                agent=agent,
                tenant_id=tenant_id,
                source=source,
            )
        finally:
            await storage.close()  # type: ignore[attr-defined]
        if n == 0:
            err_console.print("[yellow]⚠[/yellow] no chunks matched; nothing deleted.")
        else:
            console.print(f"[green]✓[/green] deleted {n} chunk(s).")

    asyncio.run(_run())
