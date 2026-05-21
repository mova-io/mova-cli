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
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from movate.cli._next_steps import mdk_bin_name
from movate.cli._progress import progress_bar
from movate.kb.embed import DEFAULT_EMBEDDING_MODEL

console = Console()
err_console = Console(stderr=True)


kb_app = typer.Typer(
    name="kb",
    help=(
        "Knowledge-base ingest + search. "
        "Run [bold]mdk kb[/bold] with no arguments for an interactive guided menu."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)


# ---------------------------------------------------------------------------
# Guided KB wizard — shown when `mdk kb` is run with no subcommand
# ---------------------------------------------------------------------------

def _kb_wizard_detect_agents(project_root: Path) -> list[tuple[str, Path]]:
    """Return [(agent_name, kb_dir)] for every agent that *could* use a KB
    (has an agent.yaml), regardless of whether the kb/ dir is populated yet.
    Also includes the project-level kb/ dir as "__shared__" if it exists.
    """
    candidates: list[tuple[str, Path]] = []

    project_kb = project_root / "kb"
    if project_kb.is_dir():
        candidates.append(("__shared__ (project-level kb/)", project_kb))

    agents_dir = project_root / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            if not (agent_dir / "agent.yaml").is_file():
                continue
            candidates.append((agent_dir.name, agent_dir / "kb"))

    return candidates


def _prompt_agent_picker(verb: str = "work with") -> str | None:
    """Interactive agent picker used by per-subcommand guided helpers.

    Returns the canonical agent name (suitable as a CLI argument), or
    ``None`` when no project was found, no agents were detected, or the
    operator cancelled.

    Non-TTY: prints the list of available agents so the operator can see
    what choices exist, then returns ``None`` — callers should emit an
    "agent argument required" error.
    """
    from movate.cli._resolve import walk_up_for_project_root  # noqa: PLC0415

    project_root = walk_up_for_project_root()
    if project_root is None:
        err_console.print(
            "[yellow]⚠[/yellow]  No project found. "
            "Run [bold]mdk init --project <name>[/bold] to create one."
        )
        return None

    agents = _kb_wizard_detect_agents(project_root)
    if not agents:
        err_console.print(
            "[yellow]⚠[/yellow]  No agents found under [bold]agents/[/bold].\n"
            "  Run [bold]mdk add rag-qa[/bold] to scaffold a KB-enabled agent."
        )
        return None

    if len(agents) == 1:
        agent_name, _ = agents[0]
        agent_arg = "__shared__" if agent_name.startswith("__shared__") else agent_name
        console.print(f"[dim]Agent:[/dim]  [bold]{agent_arg}[/bold]")
        return agent_arg

    # Multiple agents — show numbered picker with a ✓ indicator when the
    # kb/ directory is already populated.
    console.print(f"[bold]Which agent would you like to {verb}?[/bold]")
    for i, (name, kb_path) in enumerate(agents, start=1):
        try:
            kb_note = " [green]✓[/green]" if kb_path.is_dir() and any(kb_path.iterdir()) else ""
        except PermissionError:
            kb_note = ""
        display = "__shared__" if name.startswith("__shared__") else name
        console.print(f"  [bold cyan][{i}][/bold cyan]  {display}{kb_note}")
    console.print(r"  [bold cyan]\[q][/bold cyan]  Cancel")
    console.print()

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        err_console.print(
            "[yellow]⚠[/yellow]  Multiple agents found — pass the agent name explicitly:\n"
            "  [bold]mdk kb <subcommand> <agent>[/bold]"
        )
        return None

    try:
        pick = Prompt.ask(
            "[bold]Pick agent[/bold]",
            choices=[str(i) for i in range(1, len(agents) + 1)] + ["q"],
            default="q",
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        return None

    if pick == "q":
        return None

    agent_name, _ = agents[int(pick) - 1]
    return "__shared__" if agent_name.startswith("__shared__") else agent_name


def _kb_guided_wizard() -> None:
    """Interactive guided menu for the most common KB operations.

    Runs when the user types `mdk kb` with no subcommand.  Detects the
    current project, lists agents, and offers a numbered action menu —
    no need to remember argument order.
    """
    from movate.cli._resolve import walk_up_for_project_root  # noqa: PLC0415

    bin_name = mdk_bin_name()

    console.print()
    console.print(
        Panel.fit(
            "[bold]Knowledge Base Manager[/bold]\n"
            "[dim]Ingest documents · Search chunks · View stats · Clear index[/dim]",
            border_style="cyan",
        )
    )
    console.print()

    # ── 1. Locate project root ──────────────────────────────────────────────
    project_root = walk_up_for_project_root()
    if project_root is None:
        console.print(
            "[yellow]⚠[/yellow]  No project found in this directory or any parent.\n"
            "  Run [bold]mdk init --project <name>[/bold] to create one."
        )
        return

    # ── 2. Find available agents ────────────────────────────────────────────
    agents = _kb_wizard_detect_agents(project_root)
    if not agents:
        console.print(
            "[yellow]⚠[/yellow]  No agents found under [bold]agents/[/bold].\n"
            "  Run [bold]mdk add rag-qa[/bold] to scaffold a KB-enabled agent."
        )
        return

    # ── 3. Agent picker ─────────────────────────────────────────────────────
    if len(agents) == 1:
        agent_name, agent_kb_dir = agents[0]
        console.print(f"[dim]Agent:[/dim]  [bold]{agent_name}[/bold]")
        console.print()
    else:
        console.print("[bold]Select an agent:[/bold]")
        for i, (name, _) in enumerate(agents, start=1):
            console.print(f"  [bold cyan][{i}][/bold cyan]  {name}")
        console.print(r"  [bold cyan]\[s][/bold cyan]  Exit")
        console.print()

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return  # non-TTY: show list only

        try:
            pick = Prompt.ask(
                "[bold]Pick agent[/bold]",
                choices=[str(i) for i in range(1, len(agents) + 1)] + ["s"],
                default="s",
                show_choices=False,
            )
        except (KeyboardInterrupt, EOFError):
            return
        if pick == "s":
            return
        agent_name, agent_kb_dir = agents[int(pick) - 1]

    # Strip the display-only suffix from __shared__
    agent_arg = "__shared__" if agent_name.startswith("__shared__") else agent_name

    # ── 4. Action loop ──────────────────────────────────────────────────────
    while True:
        # KB dir exists and is non-empty?
        kb_populated = agent_kb_dir.is_dir() and any(agent_kb_dir.iterdir())

        rel_kb = agent_kb_dir.relative_to(project_root)
        console.print("[bold]What would you like to do?[/bold]")
        console.print(
            f"  [bold cyan][1][/bold cyan]  Ingest KB files"
            f"   [dim]{bin_name} kb ingest {agent_arg} {rel_kb}[/dim]"
        )
        console.print(
            f"  [bold cyan][2][/bold cyan]  Search the KB"
            f"   [dim]{bin_name} kb search {agent_arg} '<question>'[/dim]"
        )
        console.print(
            f"  [bold cyan][3][/bold cyan]  KB stats"
            f"   [dim]{bin_name} kb stats {agent_arg} --by-source[/dim]"
        )
        console.print(
            f"  [bold cyan][4][/bold cyan]  Ingest all agents"
            f"   [dim]{bin_name} kb ingest-all[/dim]"
        )
        console.print(
            f"  [bold cyan][5][/bold cyan]  List KB chunks"
            f"   [dim]{bin_name} kb list {agent_arg}[/dim]"
        )
        console.print(
            f"  [bold cyan][6][/bold cyan]  Clear KB index"
            f"   [dim]{bin_name} kb clear {agent_arg}[/dim]"
        )
        console.print(r"  [bold cyan]\[s][/bold cyan]  Exit")
        console.print()

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return  # non-TTY: printed menu, exit cleanly

        try:
            action = Prompt.ask(
                "[bold]Pick action[/bold]",
                choices=["1", "2", "3", "4", "5", "6", "s"],
                default="s",
                show_choices=False,
            )
        except (KeyboardInterrupt, EOFError):
            return

        if action == "s":
            return

        # ── Build argv for the chosen action ──────────────────────────────
        if action == "1":
            # Ingest — prompt for path, default to the agent's kb/ dir
            default_path = (
                str(agent_kb_dir.relative_to(project_root))
                if agent_kb_dir.is_dir()
                else f"agents/{agent_arg}/kb/"
            )
            try:
                path_str = Prompt.ask(
                    "[bold]Path to ingest[/bold]",
                    default=default_path,
                )
            except (KeyboardInterrupt, EOFError):
                return

            # Offer --dry-run preview first if kb dir is populated
            if kb_populated:
                try:
                    dry = Prompt.ask(
                        "[bold]Preview chunk counts first?[/bold] (dry-run)",
                        choices=["y", "n"],
                        default="y",
                        show_choices=True,
                    )
                except (KeyboardInterrupt, EOFError):
                    dry = "n"
                if dry == "y":
                    argv = [bin_name, "kb", "ingest", agent_arg, path_str, "--dry-run"]
                    console.print(f"\n[dim]$ {' '.join(argv)}[/dim]\n")
                    subprocess.run(argv, check=False)
                    console.print()
                    try:
                        proceed = Prompt.ask(
                            "[bold]Proceed with real ingest?[/bold]",
                            choices=["y", "n"],
                            default="y",
                            show_choices=True,
                        )
                    except (KeyboardInterrupt, EOFError):
                        proceed = "n"
                    if proceed != "y":
                        console.print()
                        continue

            argv = [bin_name, "kb", "ingest", agent_arg, path_str]

        elif action == "2":
            # Search — prompt for question
            try:
                question = Prompt.ask("[bold]Search question[/bold]")
            except (KeyboardInterrupt, EOFError):
                return
            if not question.strip():
                console.print("[yellow]Empty question — skipping.[/yellow]\n")
                continue
            argv = [bin_name, "kb", "search", agent_arg, question, "--k", "5"]

        elif action == "3":
            argv = [bin_name, "kb", "stats", agent_arg, "--by-source"]

        elif action == "4":
            argv = [bin_name, "kb", "ingest-all"]

        elif action == "5":
            argv = [bin_name, "kb", "list", agent_arg]

        elif action == "6":
            # Clear — require explicit confirmation
            console.print(
                f"\n[yellow]⚠[/yellow]  This will delete [bold]all[/bold] KB chunks for "
                f"[bold]{agent_arg}[/bold]."
            )
            try:
                confirm = Prompt.ask(
                    "[bold]Are you sure?[/bold]",
                    choices=["y", "n"],
                    default="n",
                    show_choices=True,
                )
            except (KeyboardInterrupt, EOFError):
                confirm = "n"
            if confirm != "y":
                console.print("[dim]Cancelled.[/dim]\n")
                continue
            argv = [bin_name, "kb", "clear", agent_arg, "--yes"]

        else:
            return

        # ── Execute ────────────────────────────────────────────────────────
        console.print(f"\n[dim]$ {' '.join(argv)}[/dim]\n")
        try:
            subprocess.run(argv, check=False)
        except FileNotFoundError:
            err_console.print(
                f"[yellow]⚠[/yellow] couldn't run [bold]{argv[0]}[/bold] — "
                "try running the command manually."
            )

        # ── Loop: another action? ──────────────────────────────────────────
        console.print()
        try:
            again = Prompt.ask(
                "[bold]Another KB action?[/bold]",
                choices=["y", "n"],
                default="n",
                show_choices=True,
            )
        except (KeyboardInterrupt, EOFError):
            return
        if again != "y":
            return
        console.print()


@kb_app.callback()
def kb_root(ctx: typer.Context) -> None:
    """Knowledge-base ingest + search.

    Run [bold]mdk kb[/bold] with no arguments for a guided interactive menu.
    Add a subcommand ([bold]ingest[/bold], [bold]search[/bold], [bold]stats[/bold],
    [bold]list[/bold], [bold]ingest-all[/bold], [bold]clear[/bold]) to run directly.
    """
    if ctx.invoked_subcommand is None:
        _kb_guided_wizard()


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


def _estimate_embedding_cost(files: list[Path]) -> float:
    """Rough cost estimate for embedding all files (text-embedding-3-small pricing).

    Heuristic: read each file, count chars, convert to tokens (~4 chars/token),
    apply $0.02/1M tokens. Returns float USD. Binary files (PDFs) are estimated
    by byte count as a proxy; actual token count will differ.
    """
    total_chars = 0
    for file_path in files:
        try:
            # Try UTF-8 text first; fall back to byte count for binary files.
            try:
                total_chars += len(file_path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, OSError):
                total_chars += file_path.stat().st_size
        except OSError:
            pass
    est_tokens = total_chars / 4
    return est_tokens * 0.02 / 1_000_000


@kb_app.command("ingest")
def ingest(
    agent: str | None = typer.Argument(
        None,
        help=(
            "Agent name (must match a directory under ./agents/ — "
            "we don't enforce this at the storage layer, but the "
            "skill-side lookup at run time scopes by agent so a "
            "mismatch returns no results). Omit for interactive picker."
        ),
    ),
    path: Path | None = typer.Argument(
        None,
        help=(
            "File or directory to ingest. Directories are walked "
            "recursively; supported formats: .md, .txt, .pdf, .docx, "
            ".html, .png, .jpg, .jpeg, .tiff. "
            "Hidden dirs (.git, .venv) skipped. "
            "Defaults to agents/<agent>/kb/ when omitted."
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
    changed_only: bool = typer.Option(
        False,
        "--changed-only",
        help=(
            "Skip files whose content hasn't changed since last ingest. "
            "Compares file mtime against the most recent chunk's created_at "
            "for that source path. Useful in CI to avoid re-embedding unchanged docs."
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

    Both arguments are optional — omit them to get an interactive picker
    that auto-detects agents in the current project and defaults the
    path to ``agents/<agent>/kb/``.

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

    # ── Interactive guided helpers when arguments are omitted ──────────────
    if agent is None:
        agent = _prompt_agent_picker(verb="ingest files for")
        if agent is None:
            raise typer.Exit(code=1)

    if path is None:
        from movate.cli._resolve import walk_up_for_project_root  # noqa: PLC0415

        project_root = walk_up_for_project_root()
        default_path = (
            str(project_root / "agents" / agent / "kb")
            if project_root
            else f"agents/{agent}/kb"
        )
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                path_str = Prompt.ask(
                    "[bold]Path to ingest[/bold]",
                    default=default_path,
                )
            except (KeyboardInterrupt, EOFError):
                raise typer.Exit(code=0)  # noqa: B904
            path = Path(path_str)
        else:
            path = Path(default_path)
        console.print()

    # Manual existence check (Typer's ``exists=`` was removed so we can
    # accept None and fill in the default above).
    if not path.exists():
        err_console.print(
            f"[red]✗[/red]  Path not found: [bold]{path}[/bold]\n"
            "  Create the directory and add documents, then run ingest again."
        )
        raise typer.Exit(code=2)
    # ── End guided helpers ─────────────────────────────────────────────────

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

    # Guard: warn if the path looks like an agent root (has agent.yaml) rather
    # than a kb/ directory.  A common mistake is `mdk kb ingest rag-qa agents/rag-qa`
    # which picks up prompt.md / contexts/*.md alongside the actual KB docs and
    # pollutes the search index.  The correct path is `agents/rag-qa/kb/`.
    if (path / "agent.yaml").is_file():
        kb_subdir = path / "kb"
        hint = f"[bold]mdk kb ingest {agent} {path / 'kb'}[/bold]"
        if kb_subdir.is_dir():
            err_console.print(
                f"[yellow]⚠[/yellow]  [bold]{path}[/bold] looks like an agent root "
                f"(it contains [bold]agent.yaml[/bold]).\n"
                f"  Ingesting the full agent directory will include [bold]prompt.md[/bold],\n"
                f"  context files, and schema files — not just your KB documents.\n\n"
                f"  Did you mean to ingest just the kb/ subfolder?\n"
                f"  {hint}"
            )
            raise typer.Exit(code=2)
        else:
            err_console.print(
                f"[yellow]⚠[/yellow]  [bold]{path}[/bold] is an agent root with no "
                f"[bold]kb/[/bold] subdirectory yet.\n"
                f"  Create it and drop your documents there first:\n"
                f"  [bold]mkdir -p {path / 'kb'}[/bold]"
            )
            raise typer.Exit(code=2)

    if dry_run:
        # No storage writes, no embedding calls — just walk + chunk.
        # Renders the same table shape so the operator can compare
        # what they're about to pay for against the existing chunks.
        _run_dry(path=path, agent=agent)
        return

    async def _run() -> None:
        from datetime import UTC, datetime  # noqa: PLC0415

        from movate.kb.ingest import find_files, ingest_path  # noqa: PLC0415

        files_to_ingest = find_files(path)
        total_files = len(files_to_ingest)

        console.print(f"[bold cyan]Ingesting[/bold cyan] {path} -> agent [bold]{agent}[/bold]…")
        if clean_source:
            console.print(
                "[dim]--clean-source: deleting existing chunks before re-ingest[/dim]"
            )

        if total_files > 0:
            est_cost = _estimate_embedding_cost(files_to_ingest)
            if est_cost > 0.0:
                console.print(
                    f"[dim]→ ~${est_cost:.5f} estimated embedding cost "
                    f"(text-embedding-3-small) · Ctrl-C to abort[/dim]"
                )

        storage = await _build_storage()
        summaries: list[object] = []
        try:
            if total_files == 0:
                pass  # nothing to ingest; skip progress bar
            elif changed_only:
                # Loop files individually, checking mtime vs last chunk created_at.
                with progress_bar(
                    description="Ingesting", total=total_files, transient=False
                ) as advance:
                    for i, file_path in enumerate(files_to_ingest):
                        source_uri = str(file_path.resolve())
                        # Check whether this file has changed since last ingest.
                        skip = False
                        try:
                            existing = await storage.list_kb_chunks(  # type: ignore[attr-defined]
                                agent=agent,
                                tenant_id=tenant_id,
                                source=source_uri,
                                limit=1,
                            )
                            if existing:
                                chunk = existing[0]
                                created_at_str: str = getattr(chunk, "created_at", "") or ""
                                if created_at_str:
                                    chunk_ts = datetime.fromisoformat(
                                        created_at_str.removesuffix("Z")
                                    ).replace(tzinfo=UTC).timestamp()
                                    if chunk_ts > file_path.stat().st_mtime:
                                        skip = True
                        except Exception:
                            pass  # on any error, proceed with ingest
                        advance(suffix=f" [cyan]{file_path.name}[/cyan]  [{i + 1}/{total_files}]")
                        if skip:
                            console.print(
                                f"  [dim]→ skipped (unchanged): {file_path.name}[/dim]"
                            )
                            continue
                        file_summaries = await ingest_path(
                            storage=storage,  # type: ignore[arg-type]
                            path=file_path,
                            agent=agent,
                            tenant_id=tenant_id,
                            embedding_model=model,
                            api_key=api_key,
                            clean_source=clean_source,
                        )
                        summaries.extend(file_summaries)
            else:
                with progress_bar(
                    description="Ingesting", total=total_files, transient=False
                ) as advance:
                    def _on_file(name: str, current: int, total: int) -> None:
                        advance(suffix=f" [cyan]{name}[/cyan]  [{current}/{total}]")

                    file_summaries = await ingest_path(
                        storage=storage,  # type: ignore[arg-type]
                        path=path,
                        agent=agent,
                        tenant_id=tenant_id,
                        embedding_model=model,
                        api_key=api_key,
                        clean_source=clean_source,
                        on_file_start=_on_file,
                    )
                    summaries.extend(file_summaries)
        finally:
            await storage.close()  # type: ignore[attr-defined]

        if not summaries:
            if total_files == 0:
                console.print(
                    "[yellow]⚠[/yellow] no ingestible files found under "
                    f"[bold]{path}[/bold]. "
                    "Supported formats: .md .txt .pdf .docx .html .png .jpg .tiff"
                )
            else:
                # Files were found but all failed to parse — most likely OCR missing.
                _has_images = any(
                    f.suffix.lower() in {".png", ".jpg", ".jpeg", ".tiff", ".gif"}
                    for f in files_to_ingest
                )
                console.print(
                    f"[yellow]⚠[/yellow] {total_files} file(s) found but none produced text. "
                )
                if _has_images:
                    console.print(
                        "  Image files detected. If Tesseract is not installed:\n"
                        "  [bold]macOS:[/bold]  brew install tesseract\n"
                        "  [bold]Linux:[/bold]  apt-get install tesseract-ocr"
                    )
            return

        # Render a summary table — one row per source.
        show_removed = clean_source and any(
            getattr(s, "chunks_removed", 0) > 0 for s in summaries
        )
        table = Table(title=f"[bold]Ingest summary[/bold] — agent [bold]{agent}[/bold]")
        table.add_column("source", overflow="fold")
        if show_removed:
            table.add_column("removed", justify="right")
        table.add_column("chunks", justify="right")
        table.add_column("embedding model")
        for s in summaries:
            row = [getattr(s, "source", "?")]
            if show_removed:
                row.append(str(getattr(s, "chunks_removed", 0)))
            row.extend([str(getattr(s, "chunks_saved", 0)), getattr(s, "embedding_model", "")])
            table.add_row(*row)
        console.print(table)
        total = sum(getattr(s, "chunks_saved", 0) for s in summaries)
        console.print(f"[green]✓[/green] {total} chunks saved across {len(summaries)} file(s).")

        # Surface any files that were found but silently skipped by the parser
        # (corrupt files, parse errors, or — most commonly — image files when
        # the Tesseract binary is absent).
        _skip_preview = 3
        ingested_sources = {
            Path(getattr(s, "source", "")).name for s in summaries
        }
        skipped = [f for f in files_to_ingest if f.name not in ingested_sources]
        if skipped:
            image_exts = {".png", ".jpg", ".jpeg", ".tiff", ".gif"}
            image_skipped = [f for f in skipped if f.suffix.lower() in image_exts]
            other_skipped = [f for f in skipped if f.suffix.lower() not in image_exts]
            if image_skipped:
                names = ", ".join(f.name for f in image_skipped[:_skip_preview])
                ellipsis = "…" if len(image_skipped) > _skip_preview else ""
                console.print(
                    f"\n[yellow]⚠[/yellow]  {len(image_skipped)} image file(s) were skipped "
                    f"({names}{ellipsis}).\n"
                    "  OCR requires the Tesseract binary:\n"
                    "  [bold]macOS:[/bold]  brew install tesseract\n"
                    "  [bold]Linux:[/bold]  apt-get install tesseract-ocr\n"
                    "  Then re-run [bold]mdk kb ingest[/bold] to pick up the skipped files."
                )
            if other_skipped:
                names = ", ".join(f.name for f in other_skipped[:_skip_preview])
                ellipsis = "…" if len(other_skipped) > _skip_preview else ""
                console.print(
                    f"\n[yellow]⚠[/yellow]  {len(other_skipped)} file(s) produced no text "
                    f"and were skipped ({names}{ellipsis}).\n"
                    "  Possible causes: corrupt file, encrypted PDF, or unsupported format."
                )

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


def _format_age(iso_ts: str) -> str:
    """Return human-readable age string for an ISO-8601 UTC timestamp."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415
    try:
        ts = datetime.fromisoformat(iso_ts.removesuffix("Z")).replace(tzinfo=UTC)
        delta = datetime.now(UTC) - ts
        if delta < timedelta(minutes=1):
            return "just now"
        if delta < timedelta(hours=1):
            return f"{int(delta.total_seconds() // 60)}m ago"
        if delta < timedelta(days=1):
            return f"{int(delta.total_seconds() // 3600)}h ago"
        if delta < timedelta(days=30):
            return f"{delta.days}d ago"
        return ts.strftime("%Y-%m-%d")
    except Exception:
        return iso_ts[:19]  # fallback: truncated raw string


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
            "[yellow]⚠[/yellow] no ingestible files found under "
            f"[bold]{path}[/bold]. "
            "Supported formats: .md .txt .pdf .docx .html .png .jpg .tiff"
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

    # File-type breakdown
    from collections import Counter  # noqa: PLC0415
    ext_counts = Counter(p.suffix.lower() for p in files)
    if ext_counts:
        parts = [
            f"[bold]{count}[/bold] {ext.lstrip('.').upper()}"
            for ext, count in sorted(ext_counts.items(), key=lambda x: -x[1])
        ]
        console.print(f"  [dim]types: {' · '.join(parts)}[/dim]")


@kb_app.command("search")
def search(
    agent: str | None = typer.Argument(
        None,
        help="Agent whose KB to search. Omit for interactive picker.",
    ),
    question: str | None = typer.Argument(
        None,
        help="Free-text question to retrieve against. Omit to be prompted.",
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

    Both arguments are optional — omit them to get an interactive picker
    that auto-detects agents and prompts for the search question.

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

    # ── Interactive guided helpers when arguments are omitted ──────────────
    if agent is None:
        agent = _prompt_agent_picker(verb="search")
        if agent is None:
            raise typer.Exit(code=1)

    if question is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                question = Prompt.ask("[bold]Search question[/bold]")
            except (KeyboardInterrupt, EOFError):
                raise typer.Exit(code=0)  # noqa: B904
            if not question.strip():
                err_console.print("[yellow]Empty question — nothing to search.[/yellow]")
                raise typer.Exit(code=1)
        else:
            err_console.print(
                "[red]✗[/red]  Missing argument: question.\n"
                "  Usage: [bold]mdk kb search <agent> '<question>'[/bold]"
            )
            raise typer.Exit(code=2)
    # ── End guided helpers ─────────────────────────────────────────────────

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
            # Short source name (last path segment) + optional page
            # number. ``metadata["page"]`` is set for PDF chunks ingested
            # after the page-aware ingest path landed (PR-CC-page).
            short_source = Path(r.chunk.source).name if r.chunk.source else "?"
            page = (r.chunk.metadata or {}).get("page")
            if page is not None:
                short_source = f"{short_source} p.{page}"
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
    agent: str | None = typer.Argument(None, help="Agent whose KB to inspect. Omit for picker."),
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

    Omit ``agent`` for an interactive picker.
    """
    # ── Interactive guided helper ──────────────────────────────────────────
    if agent is None:
        agent = _prompt_agent_picker(verb="list chunks for")
        if agent is None:
            raise typer.Exit(code=1)
    # ── End guided helper ──────────────────────────────────────────────────

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
    agent: str | None = typer.Argument(None, help="Agent whose KB to summarize. Omit for picker."),
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

    Omit ``agent`` for an interactive picker.
    """
    # ── Interactive guided helper ──────────────────────────────────────────
    if agent is None:
        agent = _prompt_agent_picker(verb="view stats for")
        if agent is None:
            raise typer.Exit(code=1)
    # ── End guided helper ──────────────────────────────────────────────────

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

        # Last ingested: max(created_at) across all chunks
        if chunks:
            last_ts = max(
                (c.created_at for c in chunks if getattr(c, "created_at", "")),
                default="",
            )
            if last_ts:
                # Parse ISO-8601 and render as "3 days ago" or absolute if > 30 days
                ts_str = last_ts.isoformat() if hasattr(last_ts, "isoformat") else str(last_ts)
                _last_ingested_str = _format_age(ts_str)
            else:
                _last_ingested_str = "unknown"
        else:
            _last_ingested_str = "unknown"

        # Top-level summary.
        console.print(
            f"\n[bold]KB summary[/bold] — agent [bold]{agent}[/bold] "
            f"(tenant [dim]{tenant_id}[/dim])"
        )
        console.print(f"  total chunks: [bold]{len(chunks)}[/bold]")
        console.print(f"  last ingested: [bold]{_last_ingested_str}[/bold]")
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


# ---------------------------------------------------------------------------
# `mdk kb ingest-all` — scan the whole project and ingest every KB dir
# ---------------------------------------------------------------------------

# Conventional sub-directory name inside each agent folder that holds
# KB documents. Operators drop PDFs / Markdown / DOCX here and
# `ingest-all` picks them up automatically.
_AGENT_KB_SUBDIR = "kb"
# Project-level shared KB directory (project root / kb/).
_PROJECT_KB_DIR = "kb"


def _discover_ingest_targets(project: Path) -> list[tuple[str, Path]]:
    """Return ``[(agent_name, kb_dir), ...]`` for every agent that has
    a non-empty ``kb/`` sub-directory, plus a special ``__shared__``
    entry when a project-level ``kb/`` directory exists.

    Discovery rules (in order):
    1. Project-level ``<project>/kb/`` → agent name ``__shared__``
       (operator can override with ``--shared-agent``).
    2. Per-agent ``<project>/agents/<name>/kb/`` → agent name ``<name>``.

    Hidden dirs and empty kb directories are silently skipped.
    """
    from movate.kb.ingest import find_files  # noqa: PLC0415

    targets: list[tuple[str, Path]] = []

    # Project-level kb/
    project_kb = (project / _PROJECT_KB_DIR).resolve()
    if project_kb.is_dir() and find_files(project_kb):
        targets.append(("__shared__", project_kb))

    # Per-agent kb/
    agents_dir = project / "agents"
    if agents_dir.is_dir():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            if not (agent_dir / "agent.yaml").is_file():
                continue
            kb_dir = (agent_dir / _AGENT_KB_SUBDIR).resolve()
            if kb_dir.is_dir() and find_files(kb_dir):
                targets.append((agent_dir.name, kb_dir))

    return targets


@kb_app.command("ingest-all")
def ingest_all(
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help=(
            "Project root to scan. Defaults to the current directory. "
            "Looks for ``kb/`` at the project root and ``agents/<name>/kb/`` "
            "for each agent."
        ),
    ),
    shared_agent: str = typer.Option(
        "__shared__",
        "--shared-agent",
        help=(
            "Agent name to use for files ingested from the project-level ``kb/`` "
            "directory. Defaults to [bold]__shared__[/bold]. Override when you "
            "want the shared KB scoped to a specific agent at search time."
        ),
    ),
    model: str = typer.Option(
        DEFAULT_EMBEDDING_MODEL,
        "--model",
        help=(
            "Embedding model for all ingested files. Bare names go directly to "
            "OpenAI; any ``provider/model`` string is routed through LiteLLM."
        ),
    ),
    api_key_env: str = typer.Option(
        "OPENAI_API_KEY",
        "--api-key-env",
        help="Env var holding the API key for embedding calls.",
    ),
    tenant_id: str = typer.Option(
        _DEFAULT_TENANT,
        "--tenant-id",
        help="Tenant scope. Defaults to 'local'.",
    ),
    clean_source: bool = typer.Option(
        False,
        "--clean-source",
        help=(
            "Delete existing chunks for each source file before re-ingesting. "
            "Use when updating documents to remove stale paragraphs."
        ),
    ),
    changed_only: bool = typer.Option(
        False,
        "--changed-only",
        help=(
            "Skip files whose content hasn't changed since last ingest. "
            "Compares file mtime against the most recent chunk's created_at "
            "for that source path. Useful in CI to avoid re-embedding unchanged docs."
        ),
    ),
    watch_mode: bool = typer.Option(
        False,
        "--watch",
        help=(
            "Watch KB directories for file changes and re-ingest automatically. "
            "Polls every 2 seconds. Ctrl-C to stop."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Preview what WOULD be ingested without calling the embedding API "
            "or writing to storage. Prints a table of discovered directories "
            "and file counts."
        ),
    ),
    ocr_lang: str = typer.Option(
        "",
        "--ocr-lang",
        help="Tesseract language code(s) for scanned PDFs / images (e.g. 'eng+fra').",
    ),
    ocr_backend: str = typer.Option(
        "",
        "--ocr-backend",
        help="OCR engine: 'tesseract' (default) or 'easyocr'.",
    ),
) -> None:
    """Scan the project and ingest every KB directory found.

    Looks in two places:

    \b
    1. ``<project>/kb/``              → scoped to ``--shared-agent`` (default: __shared__)
    2. ``<project>/agents/<name>/kb/`` → scoped to agent ``<name>``

    Each directory is walked recursively; files with extensions
    ``.md``, ``.markdown``, ``.txt``, ``.pdf``, ``.docx``, ``.html``,
    ``.png``, ``.jpg``, ``.jpeg``, ``.tiff`` are ingested. Hidden
    directories (``.git``, ``.venv``) are skipped.

    [bold]Examples:[/bold]

      [dim]# Ingest every KB dir in the current project[/dim]
      $ mdk kb ingest-all

      [dim]# Preview what would be ingested (no API calls)[/dim]
      $ mdk kb ingest-all --dry-run

      [dim]# Re-ingest after updating documents[/dim]
      $ mdk kb ingest-all --clean-source

      [dim]# Scope the project-level kb/ to a specific agent[/dim]
      $ mdk kb ingest-all --shared-agent rag-qa
    """
    import os  # noqa: PLC0415

    if ocr_lang:
        os.environ["MOVATE_OCR_LANG"] = ocr_lang
    if ocr_backend:
        os.environ["MOVATE_OCR_BACKEND"] = ocr_backend

    project_root = project.resolve()
    if not project_root.is_dir():
        err_console.print(f"[red]✗[/red] project path not found: {project_root}")
        raise typer.Exit(code=2)

    targets = _discover_ingest_targets(project_root)

    # Remap __shared__ to the operator's chosen agent name.
    targets = [
        (shared_agent if agent == "__shared__" else agent, kb_dir)
        for agent, kb_dir in targets
    ]

    if not targets:
        console.print(
            "[yellow]⚠[/yellow] no KB directories found.\n"
            "[dim]Create one of:\n"
            f"  {project_root / 'kb' / '<file>'}  (project-level, shared)\n"
            f"  {project_root / 'agents' / '<agent>' / 'kb' / '<file>'}  (per-agent)[/dim]"
        )
        raise typer.Exit(code=0)

    from movate.kb.ingest import find_files  # noqa: PLC0415

    if dry_run:
        table = Table(title="[bold]Discovered KB directories[/bold] (dry run — no changes)")
        table.add_column("agent", style="bold cyan")
        table.add_column("directory", overflow="fold")
        table.add_column("files", justify="right")
        for agent_name, kb_dir in targets:
            files = find_files(kb_dir)
            table.add_row(agent_name, str(kb_dir), str(len(files)))
        console.print(table)
        all_files: list[Path] = []
        for _, kb_dir in targets:
            all_files.extend(find_files(kb_dir))
        total_files = len(all_files)
        console.print(
            f"[dim]{len(targets)} KB director{'y' if len(targets) == 1 else 'ies'}, "
            f"{total_files} file(s) would be ingested.[/dim]"
        )
        # File-type breakdown
        from collections import Counter  # noqa: PLC0415
        ext_counts = Counter(p.suffix.lower() for p in all_files)
        if ext_counts:
            parts = [
                f"[bold]{count}[/bold] {ext.lstrip('.').upper()}"
                for ext, count in sorted(ext_counts.items(), key=lambda x: -x[1])
            ]
            console.print(f"  [dim]types: {' · '.join(parts)}[/dim]")
        return

    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        err_console.print(
            f"[red]✗[/red] no API key found in [bold]${api_key_env}[/bold]. "
            "Set the env var or pass [bold]--api-key-env[/bold] to point at "
            "the correct env var for your embedding provider. Pass "
            "[bold]--dry-run[/bold] to preview without embedding."
        )
        raise typer.Exit(code=2)

    async def _ingest_one_file_standalone(
        *,
        file_path: Path,
        agent_name: str,
        tenant_id: str,
        model: str,
        api_key: str,
        clean_source: bool,
    ) -> None:
        """Build storage, ingest a single file, print chunk count, close storage."""
        from movate.kb.ingest import ingest_path  # noqa: PLC0415

        storage = await _build_storage()
        try:
            summaries = await ingest_path(
                storage=storage,  # type: ignore[arg-type]
                path=file_path,
                agent=agent_name,
                tenant_id=tenant_id,
                embedding_model=model,
                api_key=api_key,
                clean_source=clean_source,
            )
        finally:
            await storage.close()  # type: ignore[attr-defined]
        chunks = sum(getattr(s, "chunks_saved", 0) for s in summaries)
        console.print(f"  [green]✓[/green] {file_path.name}: {chunks} chunks saved.")

    async def _run() -> None:
        from datetime import UTC, datetime  # noqa: PLC0415

        from movate.kb.ingest import find_files, ingest_path  # noqa: PLC0415

        # Compute total file count across all targets for the progress bar.
        all_files: list[tuple[str, Path]] = []
        for agent_name, kb_dir in targets:
            for f in find_files(kb_dir):
                all_files.append((agent_name, f))
        total_files = len(all_files)

        if total_files > 0:
            all_paths = [f for _, f in all_files]
            est_cost = _estimate_embedding_cost(all_paths)
            if est_cost > 0.0:
                console.print(
                    f"[dim]→ ~${est_cost:.5f} estimated embedding cost "
                    f"(text-embedding-3-small) · Ctrl-C to abort[/dim]"
                )

        storage = await _build_storage()
        all_summaries: list[tuple[str, object]] = []  # [(agent_name, IngestSummary)]
        try:
            if total_files == 0:
                pass  # nothing to ingest
            elif changed_only:
                with progress_bar(
                    description="Ingesting", total=total_files, transient=False
                ) as advance:
                    for i, (agent_name, file_path) in enumerate(all_files):
                        source_uri = str(file_path.resolve())
                        skip = False
                        try:
                            existing = await storage.list_kb_chunks(  # type: ignore[attr-defined]
                                agent=agent_name,
                                tenant_id=tenant_id,
                                source=source_uri,
                                limit=1,
                            )
                            if existing:
                                chunk = existing[0]
                                created_at_str: str = getattr(chunk, "created_at", "") or ""
                                if created_at_str:
                                    chunk_ts = datetime.fromisoformat(
                                        created_at_str.removesuffix("Z")
                                    ).replace(tzinfo=UTC).timestamp()
                                    if chunk_ts > file_path.stat().st_mtime:
                                        skip = True
                        except Exception:
                            pass
                        advance(
                            suffix=f" [cyan]{file_path.name}[/cyan]  [{i + 1}/{total_files}]"
                        )
                        if skip:
                            console.print(
                                f"  [dim]→ skipped (unchanged): {file_path.name}[/dim]"
                            )
                            continue
                        file_summaries = await ingest_path(
                            storage=storage,  # type: ignore[arg-type]
                            path=file_path,
                            agent=agent_name,
                            tenant_id=tenant_id,
                            embedding_model=model,
                            api_key=api_key,
                            clean_source=clean_source,
                        )
                        all_summaries.extend((agent_name, s) for s in file_summaries)
            else:
                with progress_bar(
                    description="Ingesting", total=total_files, transient=False
                ) as advance:
                    for agent_name, kb_dir in targets:
                        console.print(
                            f"[bold cyan]Ingesting[/bold cyan] "
                            f"[dim]{kb_dir}[/dim] → agent [bold]{agent_name}[/bold]…"
                        )

                        def _on_file(
                            name: str,
                            current: int,
                            total: int,
                            _agent: str = agent_name,
                        ) -> None:
                            advance(
                                suffix=f" [cyan]{name}[/cyan] ({_agent})  [{current}/{total}]"
                            )

                        dir_summaries = await ingest_path(
                            storage=storage,  # type: ignore[arg-type]
                            path=kb_dir,
                            agent=agent_name,
                            tenant_id=tenant_id,
                            embedding_model=model,
                            api_key=api_key,
                            clean_source=clean_source,
                            on_file_start=_on_file,
                        )
                        all_summaries.extend((agent_name, s) for s in dir_summaries)
        finally:
            await storage.close()  # type: ignore[attr-defined]

        if not all_summaries:
            console.print(
                "[yellow]⚠[/yellow] no ingestible files found in any KB directory."
            )
            if not watch_mode:
                return
        else:
            # Summary table — one row per source file across all agents.
            show_removed = clean_source and any(
                getattr(s, "chunks_removed", 0) > 0 for _, s in all_summaries
            )
            table = Table(title="[bold]Ingest summary[/bold]")
            table.add_column("agent", style="bold cyan")
            table.add_column("source", overflow="fold")
            if show_removed:
                table.add_column("removed", justify="right")
            table.add_column("chunks", justify="right")

            for agent_name, s in all_summaries:
                row = [agent_name, getattr(s, "source", "?")]
                if show_removed:
                    row.append(str(getattr(s, "chunks_removed", 0)))
                row.append(str(getattr(s, "chunks_saved", 0)))
                table.add_row(*row)

            console.print(table)
            total_chunks = sum(getattr(s, "chunks_saved", 0) for _, s in all_summaries)
            ingest_file_count = len(all_summaries)
            console.print(
                f"[green]✓[/green] {total_chunks} chunks saved from "
                f"{ingest_file_count} file(s) across "
                f"{len(targets)} agent(s)."
            )
            console.print(
                "[dim]Run [bold]mdk kb stats <agent>[/bold] to inspect "
                "per-agent chunk counts.[/dim]"
            )

        # --watch: poll for file changes and re-ingest automatically.
        if watch_mode:
            mtimes: dict[Path, float] = {}
            for _, kb_dir in targets:
                for f in find_files(kb_dir):
                    mtimes[f] = f.stat().st_mtime

            console.print("[dim]Watching for changes… Ctrl-C to stop.[/dim]")
            try:
                while True:
                    await asyncio.sleep(2.0)
                    changed: list[tuple[str, Path]] = []
                    for watch_agent_name, kb_dir in targets:
                        for f in find_files(kb_dir):
                            current_mtime = f.stat().st_mtime
                            if mtimes.get(f) != current_mtime:
                                mtimes[f] = current_mtime
                                changed.append((watch_agent_name, f))
                    if changed:
                        for watch_agent_name, f in changed:
                            console.print(
                                f"[dim]🔄 change detected: [bold]{f.name}[/bold] "
                                f"→ re-ingesting into [bold]{watch_agent_name}[/bold]…[/dim]"
                            )
                            await _ingest_one_file_standalone(
                                file_path=f,
                                agent_name=watch_agent_name,
                                tenant_id=tenant_id,
                                model=model,
                                api_key=api_key,
                                clean_source=True,
                            )
            except KeyboardInterrupt:
                console.print("\n[dim]Watch stopped.[/dim]")

    asyncio.run(_run())


@kb_app.command("clear")
def clear(
    agent: str | None = typer.Argument(None, help="Agent whose KB to clear. Omit for picker."),
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
    unless ``--yes`` is set.

    Omit ``agent`` for an interactive picker.
    """
    # ── Interactive guided helper ──────────────────────────────────────────
    if agent is None:
        agent = _prompt_agent_picker(verb="clear the KB for")
        if agent is None:
            raise typer.Exit(code=1)
    # ── End guided helper ──────────────────────────────────────────────────

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
