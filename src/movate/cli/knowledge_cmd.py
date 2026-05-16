"""``mdk knowledge {add, list, query}`` — RAG surface (Phase J-4).

Operates on a project-local ``knowledge.yaml`` registry + the
documents it references.

  $ mdk knowledge add ./docs/contracts-glossary.md --id contracts-glossary
  $ mdk knowledge list
  $ mdk knowledge query "what does indemnification mean?"

The MVP retriever (substring + word-overlap, no embeddings) is good
enough to demo the workflow and prove the interface. v0.8 swaps in a
vector store behind the same :class:`KnowledgeStore` Protocol — the
CLI doesn't change.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from movate.knowledge import (
    KnowledgeLoadError,
    load_knowledge,
    retrieve,
)

console = Console()
err_console = Console(stderr=True)


knowledge_app = typer.Typer(
    name="knowledge",
    help=(
        "Manage + query the project's knowledge base (Phase J-4). "
        "MVP scope: register markdown / text documents in "
        "[bold]knowledge.yaml[/bold]; query with a substring + word-"
        "overlap retriever. Production engine (embeddings, vector "
        "store, reranking) lands in v0.8 behind the same interface."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# Default location for the registry file. Operators can override per
# command with --knowledge-yaml, but the default makes the common
# case (one knowledge base at the project root) ergonomic.
_DEFAULT_KNOWLEDGE_YAML = Path("knowledge.yaml")


# ---------------------------------------------------------------------------
# Subcommand: add
# ---------------------------------------------------------------------------


@knowledge_app.command("add")
def add(
    path: Path = typer.Argument(
        ...,
        help="Path to the document file (markdown or plain text).",
    ),
    doc_id: str = typer.Option(
        "",
        "--id",
        help=(
            "Operator-supplied document id (unique within the knowledge "
            "base). Defaults to the file's basename without extension."
        ),
    ),
    description: str = typer.Option(
        "",
        "--description",
        help="One-sentence description surfaced in `mdk knowledge list`.",
    ),
    tags: str = typer.Option(
        "",
        "--tags",
        help="Comma-separated tags for filtering (future). E.g. 'sql,reference'.",
    ),
    knowledge_yaml: Path = typer.Option(
        _DEFAULT_KNOWLEDGE_YAML,
        "--knowledge-yaml",
        "-k",
        help="Registry file to update. Defaults to ./knowledge.yaml.",
    ),
) -> None:
    """Register a document in the project's knowledge.yaml.

    [bold]Examples:[/bold]

      $ mdk knowledge add ./docs/glossary.md

      [dim]# With explicit id + description + tags[/dim]
      $ mdk knowledge add ./docs/sql.md \\
          --id sql-reference \\
          --description "SQL syntax reference" \\
          --tags sql,reference

    The document is registered (not copied). Subsequent
    ``mdk knowledge query`` calls re-load the file from its
    registered path — useful when the source updates.
    """
    if not path.is_file():
        err_console.print(f"[red]✗[/red] document path does not exist: {path}")
        raise typer.Exit(code=2)

    final_id = doc_id.strip() or path.stem
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    raw = _load_yaml(knowledge_yaml)
    documents = raw.setdefault("documents", [])
    if not isinstance(documents, list):
        err_console.print(f"[red]✗[/red] 'documents' must be a list in {knowledge_yaml}")
        raise typer.Exit(code=2)

    # Replace an existing entry with the same id, otherwise append.
    rel_path = _path_relative_to(knowledge_yaml, path)
    new_entry: dict[str, object] = {"id": final_id, "path": str(rel_path)}
    if description:
        new_entry["description"] = description
    if tag_list:
        new_entry["tags"] = tag_list

    replaced = False
    for i, entry in enumerate(documents):
        if isinstance(entry, dict) and entry.get("id") == final_id:
            documents[i] = new_entry
            replaced = True
            break
    if not replaced:
        documents.append(new_entry)

    # Ensure top-level shape is set (api_version + kind) so reads
    # don't error on a freshly-created knowledge.yaml.
    raw.setdefault("api_version", "movate/v1")
    raw.setdefault("kind", "Knowledge")

    knowledge_yaml.write_text(yaml.safe_dump(raw, sort_keys=False))
    verb = "updated" if replaced else "added"
    console.print(
        f"[green]✓[/green] {verb} document [bold]{final_id}[/bold] in [bold]{knowledge_yaml}[/bold]"
    )


def _load_yaml(path: Path) -> dict[str, object]:
    """Read existing YAML, returning empty dict if absent or empty."""
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text())
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        err_console.print(
            f"[red]✗[/red] {path} root must be a mapping; got {type(loaded).__name__}"
        )
        raise typer.Exit(code=2)
    return loaded


def _path_relative_to(knowledge_yaml: Path, doc_path: Path) -> Path:
    """Write paths relative to knowledge.yaml when possible.

    Keeps the registry git-portable — absolute paths embedded in
    YAML break when the repo moves. Falls back to absolute paths
    for documents outside the knowledge.yaml directory tree.
    """
    yaml_dir = knowledge_yaml.resolve().parent
    abs_doc = doc_path.resolve()
    try:
        return abs_doc.relative_to(yaml_dir)
    except ValueError:
        return abs_doc


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


@knowledge_app.command("list")
def list_(
    knowledge_yaml: Path = typer.Option(
        _DEFAULT_KNOWLEDGE_YAML,
        "--knowledge-yaml",
        "-k",
        help="Registry file to read.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a Rich table.",
    ),
) -> None:
    """List every document registered in knowledge.yaml.

    Reads the registry without loading document bodies (cheap —
    doesn't materialise the corpus). Use ``mdk knowledge query`` to
    actually retrieve content.
    """
    if not knowledge_yaml.is_file():
        console.print(
            f"[yellow]⚠[/yellow] no [bold]{knowledge_yaml}[/bold] found. "
            f"Use [bold]mdk knowledge add[/bold] to register a document."
        )
        return

    raw = _load_yaml(knowledge_yaml)
    documents = raw.get("documents") or []
    if not isinstance(documents, list):
        err_console.print(f"[red]✗[/red] 'documents' must be a list in {knowledge_yaml}")
        raise typer.Exit(code=2)

    if json_output:
        console.print_json(json.dumps(documents))
        return

    if not documents:
        console.print(
            f"[yellow]⚠[/yellow] no documents registered in [bold]{knowledge_yaml}[/bold]. "
            f"Use [bold]mdk knowledge add[/bold] to register one."
        )
        return

    table = Table(title=f"Knowledge documents ({len(documents)})", title_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Path", style="dim")
    table.add_column("Description", style="white")
    table.add_column("Tags", style="dim")
    for entry in documents:
        if not isinstance(entry, dict):
            continue
        table.add_row(
            str(entry.get("id", "")),
            str(entry.get("path", "")),
            str(entry.get("description", "")),
            ", ".join(str(t) for t in (entry.get("tags") or [])),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Subcommand: query
# ---------------------------------------------------------------------------


@knowledge_app.command("query")
def query(
    text: str = typer.Argument(..., help="Query text — keywords or a question."),
    top_k: int = typer.Option(
        5,
        "--top-k",
        "-k",
        min=1,
        max=50,
        help="Number of results to return.",
    ),
    knowledge_yaml: Path = typer.Option(
        _DEFAULT_KNOWLEDGE_YAML,
        "--knowledge-yaml",
        help="Registry file to load (defaults to ./knowledge.yaml).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON results instead of a Rich panel — pipe-friendly.",
    ),
) -> None:
    """Retrieve the top-k chunks most relevant to the query.

    [bold]Examples:[/bold]

      $ mdk knowledge query "what does indemnification mean?"

      [dim]# Top-3 only, JSON for piping[/dim]
      $ mdk knowledge query "SQL injection" --top-k 3 --json | jq .

    MVP retriever: substring + word-overlap scoring. v0.8 swaps in
    a real vector store with no CLI change.
    """
    if not knowledge_yaml.is_file():
        err_console.print(
            f"[red]✗[/red] {knowledge_yaml} not found. "
            f"Use [bold]mdk knowledge add[/bold] to register documents first."
        )
        raise typer.Exit(code=2)

    try:
        store = load_knowledge(knowledge_yaml)
    except KnowledgeLoadError as exc:
        err_console.print(f"[red]✗[/red] failed to load knowledge base: {exc}")
        raise typer.Exit(code=2) from None

    results = retrieve(text, store, top_k=top_k)

    if json_output:
        payload = [
            {
                "doc_id": r.chunk.doc_id,
                "chunk_index": r.chunk.chunk_index,
                "score": r.score,
                "offset": r.chunk.offset,
                "snippet": r.snippet,
            }
            for r in results
        ]
        console.print_json(json.dumps(payload))
        return

    if not results:
        console.print(
            f"[yellow]⚠[/yellow] no matches for query [bold]{text!r}[/bold]. "
            f"Try different keywords or check [bold]mdk knowledge list[/bold]."
        )
        return

    console.print(f"[bold]Top {len(results)} match(es)[/bold] for [cyan]{text!r}[/cyan]")
    for i, result in enumerate(results, start=1):
        console.print()
        console.print(
            f"[bold]{i}.[/bold] [cyan]{result.chunk.doc_id}[/cyan] "
            f"[dim](chunk {result.chunk.chunk_index}, score {result.score:.2f})[/dim]"
        )
        console.print(f"   {result.snippet}")
