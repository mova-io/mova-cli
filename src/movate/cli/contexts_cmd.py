"""``mdk contexts`` — list and inspect context files wired into agents.

Shared contexts are Markdown files that get prepended to the system
prompt at run time. They live in two places:

* **Project-level** ``contexts/<name>.md`` — shared across all agents
  in the project that reference them by name in their ``agent.yaml``
  ``contexts:`` list.
* **Agent-level** ``agents/<name>/contexts/<file>.md`` — private to
  that specific agent.

``mdk contexts list`` answers "what context files exist?" and "which
agents reference them?" — the two questions that come up when debugging
"why is my agent ignoring the policy document I uploaded?".

Subcommands
-----------
``mdk contexts list``
    List all context files in the project, grouped by scope
    (project-level vs per-agent). Shows name, path, size, and
    which agents reference the context by name.
``mdk contexts show <name>``
    Print the full body of one context file. Useful when you want
    to inspect what's actually being injected into the prompt without
    grepping the raw files.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

contexts_app = typer.Typer(
    name="contexts",
    help=(
        "List and inspect shared context files injected into agent prompts. "
        "See docs/adr/002-skills-and-contexts.md for the design."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()
err_console = Console(stderr=True)

# Where project-level contexts live (relative to project root).
_PROJECT_CONTEXTS_DIR = "contexts"
# Where agent-local contexts live (relative to each agent dir).
_AGENT_CONTEXTS_SUBDIR = "contexts"
# Markdown extensions we recognise as context files.
_CONTEXT_EXTS = {".md", ".markdown", ".txt"}
# Preview snippet length (chars) shown with --verbose.
_PREVIEW_CHARS = 200
# Byte-size thresholds for _fmt_size.
_KB = 1024
_MB = 1024 * 1024
# Minimum number of path parts that triggers the agent/name split form.
_AGENT_CTX_PARTS = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_root(project: Path) -> Path:
    return project.resolve()


def _contexts_dir(project: Path) -> Path:
    return _project_root(project) / _PROJECT_CONTEXTS_DIR


def _agents_dir(project: Path) -> Path:
    return _project_root(project) / "agents"


def _fmt_size(n: int) -> str:
    """Human-readable byte size."""
    if n < _KB:
        return f"{n} B"
    if n < _MB:
        return f"{n / _KB:.1f} KB"
    return f"{n / _MB:.1f} MB"


def _collect_project_contexts(project: Path) -> list[tuple[str, Path]]:
    """Return [(name, path), ...] for files in ``contexts/``.

    Name is the stem (no extension) — matches what agent.yaml's
    ``contexts:`` list references.
    """
    cdir = _contexts_dir(project)
    if not cdir.is_dir():
        return []
    return sorted(
        (p.stem, p)
        for p in cdir.iterdir()
        if p.is_file() and p.suffix in _CONTEXT_EXTS
    )


def _collect_agent_local_contexts(agent_dir: Path) -> list[Path]:
    """Return all context files private to one agent dir."""
    cdir = agent_dir / _AGENT_CONTEXTS_SUBDIR
    if not cdir.is_dir():
        return []
    return sorted(
        p for p in cdir.iterdir() if p.is_file() and p.suffix in _CONTEXT_EXTS
    )


def _load_agent_context_refs(agent_dir: Path) -> list[str]:
    """Parse ``contexts:`` list from agent.yaml without full bundle load.

    We avoid loading the full bundle (which requires the loader, which
    may error on missing skill deps) — just YAML-parse the field.
    Returns an empty list on any parse failure.
    """
    yaml_path = agent_dir / "agent.yaml"
    if not yaml_path.is_file():
        return []
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(yaml_path.read_text()) or {}
        spec = data.get("spec", data)  # handle both bare-spec and full-document forms
        return spec.get("contexts", []) or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _body_preview(body: str) -> str:
    """Return a single-line preview snippet of *body*."""
    preview = body[:_PREVIEW_CHARS].replace("\n", " ").strip()
    if len(body) > _PREVIEW_CHARS:
        preview += "…"
    return preview


def _read_body(path: Path) -> tuple[str, str]:
    """Return (body, size_str) for *path*; returns ("", "?") on OSError."""
    try:
        body = path.read_text(encoding="utf-8")
        return body, _fmt_size(len(body.encode("utf-8")))
    except OSError:
        return "", "?"


def _render_project_contexts(
    project_contexts: list[tuple[str, Path]],
    ctx_refs: dict[str, list[str]],
    contexts_dir: Path,
    *,
    verbose: bool,
) -> None:
    """Print the project-level contexts table to *console*."""
    table = Table(
        title=f"[bold]Project-level contexts[/bold]  [dim]{contexts_dir}[/dim]",
        show_lines=False,
    )
    table.add_column("name", style="bold cyan")
    table.add_column("size", justify="right", style="dim")
    table.add_column("used by agents", overflow="fold")
    if verbose:
        table.add_column("preview", overflow="fold", max_width=60)

    for name, path in project_contexts:
        body, size = _read_body(path)
        used_by = ", ".join(ctx_refs.get(name, [])) or "[dim]—[/dim]"
        row: list[str] = [name, size, used_by]
        if verbose:
            row.append(_body_preview(body))
        table.add_row(*row)
    console.print(table)


def _render_agent_local_contexts(
    aname: str,
    adir: Path,
    local: list[Path],
    *,
    verbose: bool,
) -> None:
    """Print the per-agent local contexts table to *console*."""
    agent_table = Table(
        title=(
            f"[bold]Agent-local contexts[/bold] — [bold cyan]{aname}[/bold cyan]  "
            f"[dim]{adir / _AGENT_CONTEXTS_SUBDIR}[/dim]"
        ),
        show_lines=False,
    )
    agent_table.add_column("file", style="bold")
    agent_table.add_column("size", justify="right", style="dim")
    if verbose:
        agent_table.add_column("preview", overflow="fold", max_width=60)

    for path in local:
        body, size = _read_body(path)
        row: list[str] = [path.name, size]
        if verbose:
            row.append(_body_preview(body))
        agent_table.add_row(*row)
    console.print(agent_table)


def _resolve_agent_dirs(agents_dir: Path, agent_filter: str | None) -> list[tuple[str, Path]]:
    """Return ``[(name, path), ...]`` for agent dirs under *agents_dir*.

    If *agent_filter* is set, restrict to that single agent name.
    Returns an empty list when *agents_dir* doesn't exist.
    """
    if not agents_dir.is_dir():
        return []
    dirs = [
        (d.name, d)
        for d in sorted(agents_dir.iterdir())
        if d.is_dir() and (d / "agent.yaml").is_file()
    ]
    if agent_filter is not None:
        dirs = [(n, d) for n, d in dirs if n == agent_filter]
    return dirs


def _build_ctx_refs(
    agent_dirs: list[tuple[str, Path]],
) -> dict[str, list[str]]:
    """Return ``{context_name: [agent_name, ...]}`` from agent.yaml ``contexts:`` lists."""
    refs: dict[str, list[str]] = {}
    for _name, adir in agent_dirs:
        for ref in _load_agent_context_refs(adir):
            refs.setdefault(ref, []).append(adir.name)
    return refs


def _render_all_agent_local(
    agent_dirs: list[tuple[str, Path]],
    *,
    any_output: bool,
    verbose: bool,
) -> bool:
    """Render per-agent local context tables; return updated *any_output* flag."""
    first = True
    for aname, adir in agent_dirs:
        local = _collect_agent_local_contexts(adir)
        if not local:
            continue
        if first:
            console.print()
            first = False
            any_output = True
        _render_agent_local_contexts(aname, adir, local, verbose=verbose)
    return any_output


def _warn_no_contexts(root: Path, contexts_dir: Path) -> None:
    """Print a hint when no context files exist in the project."""
    console.print(
        f"[yellow]⚠[/yellow] no context files found under [bold]{root}[/bold]. "
        "\nCreate one with:"
    )
    console.print(
        f"  [dim]mkdir -p {contexts_dir} && "
        f'echo "# My policy" > {contexts_dir}/policy.md[/dim]'
    )
    console.print("Then reference it in [bold]agent.yaml[/bold]:")
    console.print("  [dim]contexts:\\n  - policy[/dim]")


def _warn_unused_contexts(
    project_contexts: list[tuple[str, Path]],
    ctx_refs: dict[str, list[str]],
) -> None:
    """Print a warning for project contexts not referenced by any agent."""
    unused = [name for name, _p in project_contexts if not ctx_refs.get(name)]
    if unused:
        console.print()
        console.print(
            f"[yellow]⚠[/yellow] {len(unused)} project context(s) not referenced "
            f"by any agent: {', '.join(unused)}"
        )
        console.print(
            "[dim]Add them to an agent.yaml under [bold]contexts:[/bold] to use them.[/dim]"
        )


# ---------------------------------------------------------------------------
# `mdk contexts list`
# ---------------------------------------------------------------------------


@contexts_app.command("list")
def list_contexts(
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root to scan. Defaults to the current directory.",
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        "-a",
        help="Filter to one agent's local + referenced project contexts. Omit to show all.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show the first few lines of each context file.",
    ),
) -> None:
    """List all context files in the project.

    Shows project-level contexts (``contexts/`` dir) and per-agent
    contexts (``agents/<name>/contexts/``), along with which agents
    reference each project-level context by name.

    [bold]Examples:[/bold]

      [dim]# List all contexts in the current project[/dim]
      $ mdk contexts list

      [dim]# Show only contexts relevant to one agent[/dim]
      $ mdk contexts list --agent rag-qa

      [dim]# Include a snippet preview of each file[/dim]
      $ mdk contexts list --verbose
    """
    root = _project_root(project)
    if not root.is_dir():
        err_console.print(f"[red]✗[/red] project path not found: {root}")
        raise typer.Exit(code=2)

    agents_dir = _agents_dir(project)
    agent_dirs = _resolve_agent_dirs(agents_dir, agent)

    if agent is not None and not agent_dirs:
        err_console.print(
            f"[red]✗[/red] no agent [bold]{agent}[/bold] found under {agents_dir}"
        )
        raise typer.Exit(code=2)

    project_contexts = _collect_project_contexts(project)
    ctx_refs = _build_ctx_refs(agent_dirs)

    any_output = False

    if project_contexts:
        any_output = True
        _render_project_contexts(
            project_contexts, ctx_refs, _contexts_dir(project), verbose=verbose
        )

    any_output = _render_all_agent_local(agent_dirs, any_output=any_output, verbose=verbose)

    if not any_output:
        _warn_no_contexts(root, _contexts_dir(project))

    if project_contexts and not agent:
        _warn_unused_contexts(project_contexts, ctx_refs)


# ---------------------------------------------------------------------------
# `mdk contexts show <name>`
# ---------------------------------------------------------------------------


@contexts_app.command("show")
def show_context(
    name: str = typer.Argument(
        ...,
        help="Context name (stem, no extension) for project-level; or agent/name for agent-local.",
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root. Defaults to the current directory.",
    ),
) -> None:
    """Print the full body of a context file.

    For project-level contexts, pass just the name (stem). For
    agent-local contexts, pass ``agent-name/file-name`` (e.g.
    ``rag-qa/grounded-qa-rubric``).

    [bold]Examples:[/bold]

      [dim]# Show a project-level context[/dim]
      $ mdk contexts show policy

      [dim]# Show an agent-local context[/dim]
      $ mdk contexts show rag-qa/grounded-qa-rubric
    """
    root = _project_root(project)

    # Split agent/name form.
    parts = name.split("/", 1)
    if len(parts) == _AGENT_CTX_PARTS:
        agent_name, ctx_name = parts
        candidate_dir = root / "agents" / agent_name / _AGENT_CONTEXTS_SUBDIR
    else:
        agent_name = None
        ctx_name = parts[0]
        candidate_dir = _contexts_dir(project)

    # Try each recognised extension.
    path: Path | None = None
    for ext in ["", ".md", ".markdown", ".txt"]:
        p = candidate_dir / f"{ctx_name}{ext}"
        if p.is_file():
            path = p
            break

    if path is None:
        scope = f"agent {agent_name}" if agent_name else "project"
        err_console.print(
            f"[red]✗[/red] context [bold]{ctx_name}[/bold] not found in {scope} contexts."
        )
        err_console.print(f"[dim]Looked in: {candidate_dir}[/dim]")
        raise typer.Exit(code=2)

    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        err_console.print(f"[red]✗[/red] could not read {path}: {exc}")
        raise typer.Exit(code=2) from None

    label = f"{agent_name}/{path.name}" if agent_name else path.name
    console.print(Rule(f"[bold cyan]{label}[/bold cyan]  [dim]{path}[/dim]"))
    console.print(body)
    console.print(Rule(style="dim"))
    console.print(
        f"[dim]{len(body.splitlines())} lines  "
        f"{_fmt_size(len(body.encode('utf-8')))}[/dim]"
    )


# ---------------------------------------------------------------------------
# `mdk contexts create <name>`
# ---------------------------------------------------------------------------

_CONTEXT_TEMPLATE = """\
# {name}

<!-- Replace this with your context content. This file is injected into the
     agent's system prompt at run time. See: mdk contexts list --verbose -->
"""


@contexts_app.command("create")
def create_context(
    name: str = typer.Argument(
        ...,
        help="Name for the new context file (stem, no extension).",
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        "-a",
        help=(
            "Create the context inside agents/<agent>/contexts/ instead of the "
            "project-level contexts/ directory. The agent directory must already exist."
        ),
    ),
    project: Path = typer.Option(
        Path("."),
        "--project",
        "-p",
        help="Project root. Defaults to the current directory.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite the file if it already exists.",
    ),
) -> None:
    """Create a new context file pre-filled with a minimal template.

    Creates ``contexts/<name>.md`` at the project level by default. With
    ``--agent <name>``, creates ``agents/<agent>/contexts/<name>.md`` instead.

    [bold]Examples:[/bold]

      [dim]# Create a project-level context[/dim]
      $ mdk contexts create policy

      [dim]# Create a context private to one agent[/dim]
      $ mdk contexts create grounded-rubric --agent rag-qa

      [dim]# Overwrite an existing context[/dim]
      $ mdk contexts create policy --force
    """
    root = _project_root(project)

    if agent is not None:
        # Validate that the agent directory exists.
        agent_dir = _agents_dir(project) / agent
        if not agent_dir.is_dir():
            err_console.print(
                f"[red]✗[/red] agent [bold]{agent}[/bold] not found: "
                f"{agent_dir}"
            )
            raise typer.Exit(code=2)
        target_dir = agent_dir / _AGENT_CONTEXTS_SUBDIR
    else:
        target_dir = _contexts_dir(project)

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{name}.md"

    if dest.exists() and not force:
        err_console.print(
            f"[red]✗[/red] context file already exists: [bold]{dest}[/bold]\n"
            "[dim]Pass [bold]--force[/bold] to overwrite.[/dim]"
        )
        raise typer.Exit(code=2)

    body = _CONTEXT_TEMPLATE.format(name=name)
    dest.write_text(body, encoding="utf-8")

    # Compute a display path relative to root when possible.
    try:
        display = dest.relative_to(root)
    except ValueError:
        display = dest

    console.print(f"[green]✓[/green] Created [bold]{display}[/bold]")
    console.print(
        f"[dim]Reference it in agent.yaml under "
        f"[bold]contexts: [{name}][/bold][/dim]"
    )
