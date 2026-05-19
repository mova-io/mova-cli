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
    api_key_env: str = typer.Option(
        "OPENAI_API_KEY",
        "--api-key-env",
        help=(
            "Env var holding the OpenAI key for embedding calls. Defaults to ``OPENAI_API_KEY``."
        ),
    ),
) -> None:
    """Ingest a knowledge-base file or directory into ``agent``'s KB."""
    import os  # noqa: PLC0415

    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        err_console.print(
            f"[red]✗[/red] no OpenAI API key in [bold]${api_key_env}[/bold]. "
            "Run [bold]mdk auth login openai[/bold] first."
        )
        raise typer.Exit(code=2)

    async def _run() -> None:
        from movate.kb.ingest import ingest_path  # noqa: PLC0415

        storage = await _build_storage()
        try:
            console.print(f"[bold cyan]Ingesting[/bold cyan] {path} -> agent [bold]{agent}[/bold]…")
            summaries = await ingest_path(
                storage=storage,
                path=path,
                agent=agent,
                tenant_id=tenant_id,
                api_key=api_key,
            )
        finally:
            await storage.close()  # type: ignore[attr-defined]

        if not summaries:
            console.print(
                "[yellow]⚠[/yellow] no ingestible files found (looked for .md / .markdown / .txt)."
            )
            return

        # Render a summary table — one row per source.
        table = Table(title=f"[bold]Ingest summary[/bold] — agent [bold]{agent}[/bold]")
        table.add_column("source", overflow="fold")
        table.add_column("chunks", justify="right")
        table.add_column("embedding model")
        for s in summaries:
            table.add_row(s.source, str(s.chunks_saved), s.embedding_model)
        console.print(table)
        total = sum(s.chunks_saved for s in summaries)
        console.print(f"[green]✓[/green] {total} chunks saved across {len(summaries)} file(s).")
        console.print(f'[dim]Try it: [bold]mdk kb search {agent} "your question here"[/bold][/dim]')

    asyncio.run(_run())


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
    api_key_env: str = typer.Option(
        "OPENAI_API_KEY",
        "--api-key-env",
        help="Env var holding the OpenAI key for query embedding.",
    ),
    show_full: bool = typer.Option(
        False,
        "--full",
        help="Print full chunk text (default truncates to 200 chars).",
    ),
) -> None:
    """Semantic search over ``agent``'s KB. Prints top-K with scores.

    Use this to validate that retrieval is finding the right chunks
    BEFORE running the agent end-to-end — saves the cost of agent
    iterations on a bad KB.
    """
    import os  # noqa: PLC0415

    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        err_console.print(f"[red]✗[/red] no OpenAI API key in [bold]${api_key_env}[/bold].")
        raise typer.Exit(code=2)

    async def _run() -> None:
        from movate.kb.search import search as kb_search  # noqa: PLC0415

        storage = await _build_storage()
        try:
            results = await kb_search(
                storage=storage,
                question=question,
                agent=agent,
                tenant_id=tenant_id,
                limit=k,
                api_key=api_key,
            )
        finally:
            await storage.close()  # type: ignore[attr-defined]

        if not results:
            err_console.print(
                f"[yellow]⚠[/yellow] no chunks in [bold]{agent}[/bold]'s KB "
                f"(tenant=[bold]{tenant_id}[/bold]). "
                "Did you run [bold]mdk kb ingest[/bold] first?"
            )
            return

        table = Table(
            title=(
                f'[bold]Top {len(results)} chunks[/bold] for "[italic]{question}[/italic]"'
                f" — agent [bold]{agent}[/bold]"
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
