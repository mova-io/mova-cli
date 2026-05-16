"""``mdk knowledge validate`` — corpus coverage check for KB-backed agents.

Scans a KB corpus against an agent's eval dataset and reports which
queries return zero matches. Operators use this to find gaps between
"what the evals ask" and "what the KB actually covers" before running
``mdk eval`` and wondering why scores are low.

Typical workflow:

  1. ``mdk add rag-qa`` — scaffolds an agent with a KB lookup skill.
  2. Drop your real data into ``kb/kb-lookup-corpus.json``.
  3. ``mdk knowledge validate rag-qa`` — see which eval queries miss.
  4. Add missing entries to the corpus, re-run to confirm coverage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from movate.core.config import PROJECT_MARKER_FILES

err = Console(stderr=True)
out = Console()

knowledge_app = typer.Typer(
    name="knowledge",
    help=(
        "Inspect and validate knowledge-base assets. "
        "[bold]validate[/bold] reports which eval queries "
        "return zero corpus matches."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# ---------------------------------------------------------------------------
# Corpus + scoring (mirrors impl.py, kept minimal for the CLI's read-only use)
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been",
        "i", "we", "my", "our", "you", "your", "it", "they",
        "to", "of", "in", "on", "for", "with", "at", "by", "from",
        "and", "or", "but", "as", "if", "this", "that", "these", "those",
        "how", "what", "why", "when", "where",
    }
)
_TOKEN_RE = re.compile(r"\b[a-z0-9][a-z0-9_-]*\b")
_W_TAG, _W_TITLE, _W_BODY = 5, 4, 1
_W_TITLE_EXACT_BONUS = 2


def _tokenize(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _score_entry(entry: dict[str, Any], query_tokens: set[str]) -> int:
    if not query_tokens:
        return 0
    tag_hits = sum(1 for t in entry.get("tags", []) if t.lower() in query_tokens)
    title_tokens = _tokenize(entry.get("title", ""))
    title_hits = len(title_tokens & query_tokens)
    exact = title_hits == len(query_tokens) and title_hits > 0
    title_score = _W_TITLE * title_hits * (_W_TITLE_EXACT_BONUS if exact else 1)
    body_hits = len(
        (_tokenize(entry.get("symptom", "")) | _tokenize(entry.get("resolution", "")))
        & query_tokens
    )
    return _W_TAG * tag_hits + title_score + _W_BODY * body_hits


def _has_any_match(corpus: list[dict[str, Any]], query_tokens: set[str]) -> bool:
    return any(_score_entry(e, query_tokens) > 0 for e in corpus)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _find_project_root(start: Path) -> Path | None:
    for parent in (start, *start.parents):
        if any((parent / m).is_file() for m in PROJECT_MARKER_FILES):
            return parent
    return None


def _load_corpus(corpus_path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(corpus_path.read_text())
        if not isinstance(data, list):
            err.print(f"[yellow]![/yellow] {corpus_path}: expected a JSON array, skipping")
            return []
        return [e for e in data if isinstance(e, dict)]
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        err.print(f"[red]✗[/red] could not load {corpus_path}: {exc}")
        return []


def _find_agent_dirs(agents_dir: Path) -> list[Path]:
    return sorted(
        p for p in agents_dir.iterdir() if p.is_dir() and (p / "agent.yaml").is_file()
    )


def _extract_query_from_input(inp: dict[str, Any]) -> str | None:
    """Best-effort extraction of a text query from a dataset entry's input.

    Tries ``query``, ``question``, ``text``, ``message`` in that order,
    then falls back to the first string-valued key alphabetically. Returns
    None if the input has no string fields.
    """
    for key in ("query", "question", "text", "message"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in sorted(inp):
        val = inp[key]
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _resolve_corpus_path(corpus: str | None, root: Path) -> Path:
    if corpus:
        p = Path(corpus)
        if not p.is_absolute():
            p = (root / p) if (root / p).is_file() else root / "kb" / corpus
        return p
    return root / "kb" / "kb-lookup-corpus.json"


def _resolve_agent_dirs(agent: str | None, root: Path) -> list[Path]:
    if agent:
        candidate = Path(agent)
        if candidate.is_dir() and (candidate / "agent.yaml").is_file():
            return [candidate.resolve()]
        by_name = root / "agents" / agent
        if (by_name / "agent.yaml").is_file():
            return [by_name.resolve()]
        err.print(f"[red]✗[/red] agent not found: [bold]{agent}[/bold]")
        raise typer.Exit(code=2)
    agents_base = root / "agents"
    if not agents_base.is_dir():
        err.print(
            "[red]✗[/red] no [bold]agents/[/bold] directory found. "
            "Pass an explicit agent path or run from a project root."
        )
        raise typer.Exit(code=2)
    return _find_agent_dirs(agents_base)


def _scan_dataset(dataset_path: Path, corpus: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return (hits, misses) query strings from the dataset."""
    hits: list[str] = []
    misses: list[str] = []
    for line in dataset_path.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s)
        except json.JSONDecodeError:
            continue
        inp = row.get("input", {})
        q = _extract_query_from_input(inp) if isinstance(inp, dict) else None
        if q is None:
            continue
        if _has_any_match(corpus, _tokenize(q)):
            hits.append(q)
        else:
            misses.append(q)
    return hits, misses


def _render_agent_result(
    agent_name: str,
    hits: list[str],
    misses: list[str],
    corpus_name: str,
    *,
    show_passing: bool,
) -> bool:
    """Print one agent's result; returns True if any misses were found."""
    total = len(hits) + len(misses)
    if not total:
        return False
    status = "[green]✓[/green]" if not misses else "[red]✗[/red]"
    out.print(
        f"\n{status} [bold]{agent_name}[/bold] — "
        f"{len(hits)}/{total} queries covered by corpus [dim]({corpus_name})[/dim]"
    )
    if misses or show_passing:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Status", width=6)
        table.add_column("Query")
        for q in misses:
            table.add_row("[red]miss[/red]", q)
        if show_passing:
            for q in hits:
                table.add_row("[green]hit [/green]", q)
        out.print(table)
    if misses:
        out.print(
            f"  [dim]hint: add {len(misses)} entries to [bold]{corpus_name}[/bold] "
            "covering the missing queries.[/dim]"
        )
    return bool(misses)


@knowledge_app.command("validate")
def knowledge_validate(
    agent: str = typer.Argument(
        None,
        help=(
            "Agent name or path. Omit to validate all agents in [bold]agents/[/bold] "
            "that have an eval dataset."
        ),
        metavar="AGENT",
    ),
    corpus: str = typer.Option(
        None,
        "--corpus",
        "-c",
        help=(
            "Path to corpus JSON, or a filename relative to [bold]kb/[/bold]. "
            "Defaults to [bold]kb/kb-lookup-corpus.json[/bold] in the project root."
        ),
    ),
    show_passing: bool = typer.Option(
        False,
        "--show-passing",
        help="Also show queries that DID get at least one corpus match.",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        hidden=True,
    ),
) -> None:
    """Check which eval queries return zero KB corpus matches.

    Loads the corpus and each agent's [bold]evals/dataset.jsonl[/bold],
    then runs the same keyword scoring as the kb-lookup skill. Queries that
    produce no hits indicate corpus gaps — entries you need to add before
    eval accuracy will improve.

    [bold]Examples:[/bold]

      [dim]$ mdk knowledge validate[/dim]
      [dim]$ mdk knowledge validate rag-qa[/dim]
      [dim]$ mdk knowledge validate rag-qa --corpus kb/my-corpus.json[/dim]
      [dim]$ mdk knowledge validate --show-passing[/dim]
    """
    root = Path(project_root).resolve()
    corpus_path = _resolve_corpus_path(corpus, root)

    if not corpus_path.is_file():
        err.print(
            f"[red]✗[/red] corpus not found at [bold]{corpus_path}[/bold]. "
            "[dim]Create the file or pass [bold]--corpus <path>[/bold].[/dim]"
        )
        raise typer.Exit(code=2)

    kb_corpus = _load_corpus(corpus_path)
    if not kb_corpus:
        err.print(f"[red]✗[/red] corpus at {corpus_path} is empty or unreadable.")
        raise typer.Exit(code=2)

    agent_dirs = _resolve_agent_dirs(agent, root)

    any_issues = False
    scanned = 0
    for agent_dir in agent_dirs:
        dataset_path = agent_dir / "evals" / "dataset.jsonl"
        if not dataset_path.is_file():
            continue
        scanned += 1
        hits, misses = _scan_dataset(dataset_path, kb_corpus)
        had_issues = _render_agent_result(
            agent_dir.name, hits, misses, corpus_path.name, show_passing=show_passing
        )
        any_issues = any_issues or had_issues

    if scanned == 0:
        err.print(
            "[yellow]![/yellow] no agents with eval datasets found. "
            "Run [bold]mdk eval-gen <agent>[/bold] to generate a dataset first."
        )
        raise typer.Exit(code=1)

    if not any_issues:
        out.print("\n[green]✓[/green] all eval queries have at least one corpus match.")
    else:
        raise typer.Exit(code=1)
