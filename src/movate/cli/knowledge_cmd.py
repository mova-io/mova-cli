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


@knowledge_app.command("add")
def knowledge_add(
    json_entry: str = typer.Option(
        None,
        "--json",
        "-j",
        help=(
            "JSON object with entry fields (id, title, symptom, resolution, tags). "
            "Omit to be prompted interactively."
        ),
    ),
    corpus: str = typer.Option(
        None,
        "--corpus",
        "-c",
        help=(
            "Path to corpus JSON. Defaults to [bold]kb/kb-lookup-corpus.json[/bold] "
            "in the project root."
        ),
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        hidden=True,
    ),
) -> None:
    """Append one entry to the KB corpus interactively or via --json.

    In a TTY, prompts for each required field. In CI/non-TTY, pass
    [bold]--json '{"id":"...","title":"...","resolution":"..."}' [/bold].

    Required fields: [bold]id[/bold], [bold]title[/bold], [bold]resolution[/bold].
    Optional: [bold]symptom[/bold], [bold]tags[/bold].

    [bold]Examples:[/bold]

      [dim]$ mdk knowledge add[/dim]
      [dim]$ mdk knowledge add --json '{"id":"KB-042","title":"T","resolution":"R"}'[/dim]
    """
    root = Path(project_root).resolve()
    corpus_path = _resolve_corpus_path(corpus, root)

    if json_entry is not None:
        try:
            entry: dict[str, Any] = json.loads(json_entry)
        except json.JSONDecodeError as exc:
            err.print(f"[red]✗[/red] --json value is not valid JSON: {exc}")
            raise typer.Exit(code=2) from None
        if not isinstance(entry, dict):
            err.print("[red]✗[/red] --json value must be a JSON object, not an array or scalar.")
            raise typer.Exit(code=2)
    else:
        import sys  # noqa: PLC0415

        if not sys.stdin.isatty():
            err.print(
                "[red]✗[/red] not a TTY — pass [bold]--json '{...}'[/bold] with the entry fields."
            )
            raise typer.Exit(code=2)
        out.print("[bold]Add KB corpus entry[/bold] [dim](Ctrl-C to cancel)[/dim]\n")
        entry = {}
        entry["id"] = typer.prompt("  id       (e.g. KB-001)")
        entry["title"] = typer.prompt("  title    (short description)")
        entry["symptom"] = typer.prompt("  symptom  (what the user observes)", default="")
        entry["resolution"] = typer.prompt("  resolution (how to fix)")
        raw_tags = typer.prompt("  tags     (comma-separated, optional)", default="")
        entry["tags"] = [t.strip() for t in raw_tags.split(",") if t.strip()]

    missing = [f for f in ("id", "title", "resolution") if not entry.get(f)]
    if missing:
        err.print(f"[red]✗[/red] missing required fields: {', '.join(missing)}")
        raise typer.Exit(code=2)

    # Load existing corpus (or start fresh).
    if corpus_path.is_file():
        existing = _load_corpus(corpus_path)
    else:
        corpus_path.parent.mkdir(parents=True, exist_ok=True)
        existing = []

    existing.append(entry)
    corpus_path.write_text(json.dumps(existing, indent=2))
    out.print(
        f"[green]✓[/green] added [bold]{entry['id']!r}[/bold] — "
        f"corpus now has {len(existing)} entr{'y' if len(existing) == 1 else 'ies'} "
        f"at [dim]{corpus_path}[/dim]"
    )


@knowledge_app.command("remove")
def knowledge_remove(
    entry_id: str = typer.Argument(..., help="The [bold]id[/bold] of the entry to remove."),
    corpus: str = typer.Option(
        None,
        "--corpus",
        "-c",
        help="Path to corpus JSON. Defaults to [bold]kb/kb-lookup-corpus.json[/bold].",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        hidden=True,
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Remove one entry from the KB corpus by id.

    [bold]Examples:[/bold]

      [dim]$ mdk knowledge remove KB-042[/dim]
      [dim]$ mdk knowledge remove KB-042 --yes[/dim]
    """
    root = Path(project_root).resolve()
    corpus_path = _resolve_corpus_path(corpus, root)
    if not corpus_path.is_file():
        err.print(f"[red]✗[/red] corpus not found: {corpus_path}")
        raise typer.Exit(code=2)

    entries = _load_corpus(corpus_path)
    matches = [e for e in entries if isinstance(e, dict) and e.get("id") == entry_id]
    if not matches:
        err.print(
            f"[red]✗[/red] no entry with id [bold]{entry_id!r}[/bold] found in corpus "
            f"([dim]{corpus_path}[/dim])."
        )
        raise typer.Exit(code=2)

    if not yes:
        import sys  # noqa: PLC0415

        if sys.stdin.isatty():
            typer.confirm(
                f"Remove entry {entry_id!r} from corpus ({corpus_path.name})?",
                abort=True,
            )
        else:
            err.print(
                "[red]✗[/red] not a TTY — pass [bold]--yes[/bold] to confirm removal."
            )
            raise typer.Exit(code=2)

    remaining = [e for e in entries if not (isinstance(e, dict) and e.get("id") == entry_id)]
    corpus_path.write_text(json.dumps(remaining, indent=2))
    removed_n = len(entries) - len(remaining)
    out.print(
        f"[green]✓[/green] removed [bold]{entry_id!r}[/bold] "
        f"({removed_n} entr{'y' if removed_n == 1 else 'ies'} deleted) — "
        f"corpus now has {len(remaining)} entr{'y' if len(remaining) == 1 else 'ies'}."
    )


@knowledge_app.command("edit")
def knowledge_edit(
    entry_id: str = typer.Argument(..., help="The [bold]id[/bold] of the entry to edit."),
    patch: str = typer.Option(
        ...,
        "--set",
        "-s",
        help=(
            "JSON object of fields to update, e.g. "
            "[bold]--set '{\"resolution\": \"New answer\"}' [/bold]. "
            "Only listed keys are changed; others are preserved."
        ),
    ),
    corpus: str = typer.Option(
        None,
        "--corpus",
        "-c",
        help="Path to corpus JSON. Defaults to [bold]kb/kb-lookup-corpus.json[/bold].",
    ),
    project_root: str = typer.Option(
        ".",
        "--project-root",
        envvar="MOVATE_PROJECT_ROOT",
        hidden=True,
    ),
) -> None:
    """Patch one entry in the KB corpus by id.

    Only the fields listed in [bold]--set[/bold] are updated; all
    other fields are preserved. Pass [bold]--set '{"id": "new-id"}'[/bold]
    to rename an entry.

    [bold]Examples:[/bold]

      [dim]$ mdk knowledge edit KB-042 --set '{"resolution": "Updated answer"}' [/dim]
      [dim]$ mdk knowledge edit KB-042 --set '{"tags": ["billing", "refunds"]}' [/dim]
    """
    root = Path(project_root).resolve()
    corpus_path = _resolve_corpus_path(corpus, root)
    if not corpus_path.is_file():
        err.print(f"[red]✗[/red] corpus not found: {corpus_path}")
        raise typer.Exit(code=2)

    try:
        patch_dict: dict[str, Any] = json.loads(patch)
    except json.JSONDecodeError as exc:
        err.print(f"[red]✗[/red] --set value is not valid JSON: {exc}")
        raise typer.Exit(code=2) from None
    if not isinstance(patch_dict, dict):
        err.print("[red]✗[/red] --set value must be a JSON object.")
        raise typer.Exit(code=2)

    entries = _load_corpus(corpus_path)
    updated = False
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == entry_id:
            entry.update(patch_dict)
            updated = True
            break

    if not updated:
        err.print(
            f"[red]✗[/red] no entry with id [bold]{entry_id!r}[/bold] found in corpus "
            f"([dim]{corpus_path}[/dim])."
        )
        raise typer.Exit(code=2)

    corpus_path.write_text(json.dumps(entries, indent=2))
    fields_changed = ", ".join(patch_dict.keys())
    out.print(
        f"[green]✓[/green] updated [bold]{entry_id!r}[/bold] "
        f"(fields: {fields_changed}) in [dim]{corpus_path}[/dim]."
    )
